#!/usr/bin/env python

"""Dynamics module to simulate dynamical systems with examples"""

import abc

import numpy as np
from scipy.linalg import block_diag
from scipy.optimize import approx_fprime
import sympy as sym
import torch

from decentralized.util import split_agents


class DynamicalModel(abc.ABC):
    """Simulation of a dynamical model to be applied in the iLQR solution."""

    _id = 0

    def __init__(self, n_x, n_u, dt, id=None):
        if not id:
            id = DynamicalModel._id
            DynamicalModel._id += 1

        self.n_x = n_x
        self.n_u = n_u
        self.dt = dt
        self.id = id
        self.NX_EYE = np.eye(self.n_x, dtype=np.float32)

    def __call__(self, x, u):
        """Zero-order hold to integrate continuous dynamics f"""
        return x + self.f(x, u) * self.dt
        # Single RK4 integration of continuous dynamics.
        # k1 = self.dt * self.f(x, u)
        # k2 = self.dt * self.f(x + 0.5 * k1, u)
        # k3 = self.dt * self.f(x + 0.5 * k2, u)
        # k4 = self.dt * self.f(x + k3, u)
        # x += (k1 + 2.0 * k2 + 2.0 * k3 + k4) / 6.0
        # return x

    @staticmethod
    @abc.abstractmethod
    def f():
        """Continuous derivative of dynamics with respect to time"""
        pass

    def linearize(self, x: torch.tensor, u: torch.tensor, discrete=False):
        """Compute the Jacobian linearization of the dynamics for a particular state
        and controls for all players.
        """

        A, B = torch.autograd.functional.jacobian(self.f, (x, u))

        if discrete:
            return A, B

        # Compute the discretized jacobians with euler integration.
        A = self.dt * A.reshape(self.n_x, self.n_x) + self.NX_EYE
        B = self.dt * B.reshape(self.n_x, self.n_u)
        return A, B

    @classmethod
    def _reset_ids(cls):
        cls._id = 0

    def __repr__(self):
        return f"{type(self).__name__}(n_x: {self.n_x}, n_u: {self.n_u}, id: {self.id})"


class AnalyticalModel(DynamicalModel):
    """Mix-in for analytical linearization"""

    def linearize(self, x, u):
        return self.A_num(x, u), self.B_num(x, u)


class MultiDynamicalModel(DynamicalModel):
    """Encompasses the dynamical simulation and linearization for a collection of
    DynamicalModel's
    """

    def __init__(self, submodels):
        self.submodels = submodels
        self.n_players = len(submodels)

        self.x_dims = [submodel.n_x for submodel in submodels]
        self.u_dims = [submodel.n_u for submodel in submodels]
        self.ids = [submodel.id for submodel in submodels]

        super().__init__(sum(self.x_dims), sum(self.u_dims), submodels[0].dt, -1)

    def f(self, x, u):
        """Integrate the dynamics for the combined decoupled dynamical model"""
        return torch.cat(
            [
                submodel.f(xi.flatten(), ui.flatten())
                for submodel, xi, ui in zip(self.submodels, *self.partition(x, u))
            ]
        )

    def partition(self, x, u):
        """Helper to split up the states and control for each subsystem"""
        return split_agents(x, self.x_dims), split_agents(u, self.u_dims)

    def split(self, graph):
        """Split this model into submodels dictated by the interaction graph"""
        split_dynamics = []
        for problem in graph:
            split_dynamics.append(
                MultiDynamicalModel(
                    [model for model in self.submodels if model.id in graph[problem]]
                )
            )

        return split_dynamics

    def __repr__(self):
        sub_reprs = ",\n\t".join([repr(submodel) for submodel in self.submodels])
        return f"MultiDynamicalModel(\n\t{sub_reprs}\n)"


class DoubleIntDynamics4D(DynamicalModel):
    def __init__(self, dt, *args, **kwargs):
        super().__init__(4, 2, dt, *args, **kwargs)

    @staticmethod
    def f(x, u):
        *_, vx, vy = x
        ax, ay = u
        return torch.stack([vx, vy, ax, ay])


class CarDynamics3D(DynamicalModel):
    def __init__(self, dt, *args, **kwargs):
        super().__init__(3, 2, dt, *args, **kwargs)

    @staticmethod
    def f(x, u):
        *_, theta = x
        v, omega = u
        return torch.stack([v * torch.cos(theta), v * torch.sin(theta), omega])


class UnicycleDynamics4D(DynamicalModel):
    def __init__(self, dt, *args, **kwargs):
        super().__init__(4, 2, dt, *args, **kwargs)

    @staticmethod
    def f(x, u):
        *_, v, theta = x
        a, omega = u
        return torch.stack([v * torch.cos(theta), v * torch.sin(theta), a, omega])


class UnicycleDynamics4dSymbolic(AnalyticalModel):
    def __init__(self, dt, *args, **kwargs):
        super().__init__(4, 2, dt, *args, **kwargs)

        p_x, p_y, v, theta, omega, a = sym.symbols('p_x p_y v theta omega a')
        x = sym.Matrix([p_x, p_y, v, theta])
        u = sym.Matrix([a, omega])

        x_dot = sym.Matrix([
            x[2]*sym.cos(x[3]),
            x[2]*sym.sin(x[3]),
            u[0],
            u[1],
        ])

        A = x_dot.jacobian(x)
        B = x_dot.jacobian(u)

        





class BikeDynamics5D(DynamicalModel):
    def __init__(self, dt, *args, **kwargs):
        super().__init__(5, 2, dt)

    @staticmethod
    def f(x, u):
        *_, v, theta, phi = x
        a, phi_dot = u
        return torch.stack(
            [v * torch.cos(theta), v * torch.sin(theta), a, torch.tan(phi), phi_dot]
        )


# Based off of https://github.com/anassinator/ilqr/blob/master/ilqr/dynamics.py
def linearize_finite_difference(f, x, u):
    """ "Linearization using finite difference.

    NOTE: deprecated in favor of automatic differentiation.
    """

    n_x = x.size
    jac_eps = np.sqrt(np.finfo(float).eps)

    A = np.vstack([approx_fprime(x, lambda x: f(x, u)[i], jac_eps) for i in range(n_x)])
    B = np.vstack([approx_fprime(u, lambda u: f(x, u)[i], jac_eps) for i in range(n_x)])

    return A, B


def linearize_multi(submodels, partition, x, u):
    """Compute the submodel-linearizations

    NOTE: deprecated in favor of automatic differentiation.
    """

    sub_linearizations = [
        submodel.linearize(xi.flatten(), ui.flatten())
        for submodel, xi, ui in zip(submodels, *partition(x, u))
    ]

    sub_As = [AB[0] for AB in sub_linearizations]
    sub_Bs = [AB[1] for AB in sub_linearizations]

    return block_diag(*sub_As), block_diag(*sub_Bs)
