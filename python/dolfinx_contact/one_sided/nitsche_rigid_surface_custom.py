# Copyright (C) 2021 Sarah Roggendorf
#
# SPDX-License-Identifier:    MIT

from typing import Optional, Dict, Tuple

import basix
import dolfinx.common as _common
import dolfinx.fem as _fem
import dolfinx.log as _log
import dolfinx.mesh as _mesh
import numpy as np
import ufl
from dolfinx.graph import create_adjacencylist

import dolfinx_contact
import dolfinx_contact.cpp
from dolfinx_contact.helpers import (epsilon, lame_parameters,
                                     rigid_motions_nullspace, sigma_func)

__all__ = ["nitsche_rigid_surface_custom"]
kt = dolfinx_contact.cpp.Kernel


def nitsche_rigid_surface_custom(mesh: _mesh.Mesh, mesh_data: Tuple[_mesh.MeshTags, int, int, int, int],
                                 physical_parameters: Optional[dict] = None,
                                 nitsche_parameters: Optional[Dict[str, float]] = None,
                                 vertical_displacement: float = -0.1, nitsche_bc: bool = True,
                                 quadrature_degree: int = 5, form_compiler_options: Optional[Dict] = None,
                                 jit_options: Optional[Dict] = None, petsc_options: Optional[Dict] = None,
                                 newton_options: Optional[Dict] = None):
    """
    Use custom kernel to compute the one sided contact problem with a mesh coming into contact
    with a rigid surface (meshed).

    Parameters
    ==========
    mesh
        The input mesh
    mesh_data
        A quinteplet with a mesh tag for facets and values v0, v1, v2, v3. v0 and v3
        should be the values in the mesh tags for facets to apply a Dirichlet condition
        on, where v0 corresponds to the elastic body and v2 to the rigid body. v1 is the
        value for facets which should have applied a contact condition on and v2 marks
        the potential contact surface on the rigid body.
    physical_parameters
        Optional dictionary with information about the linear elasticity problem.
        Valid (key, value) tuples are: ('E': float), ('nu', float), ('strain', bool)
    nitsche_parameters
        Optional dictionary with information about the Nitsche configuration.
        Valid (keu, value) tuples are: ('gamma', float), ('theta', float) where theta can be -1, 0 or 1 for
        skew-symmetric, penalty like or symmetric enforcement of Nitsche conditions
    vertical_displacement
        The amount of verticial displacment enforced on Dirichlet boundary
    nitsche_bc
        Use Nitche's method to enforce Dirichlet boundary conditions
    quadrature_degree
        The quadrature degree to use for the custom contact kernels
    form_compiler_options
        Parameters used in FFCX compilation of this form. Run `ffcx --help` at
        the commandline to see all available opsurface_bottomtions. Takes priority over all
        other parameter values, except for `scalar_type` which is determined by
        DOLFINX.
    jit_options
        Parameters used in CFFI JIT compilation of C code generated by FFCX.
        See https://github.com/FEniCS/dolfinx/blob/main/python/dolfinx/jit.py
        for all available parameters. Takes priority over all other parameter values.
    petsc_options
        Parameters that is passed to the linear algebra backend
        PETSc. For available choices for the 'petsc_options' kwarg,
        see the `PETSc-documentation
        <https://petsc4py.readthedocs.io/en/stable/manual/ksp/>`
    newton_options
        Dictionary with Newton-solver options. Valid (key, item) tuples are:
        ("atol", float), ("rtol", float), ("convergence_criterion", "str"),
        ("max_it", int), ("error_on_nonconvergence", bool), ("relaxation_parameter", float)
    """
    # Check input dictionaries
    if form_compiler_options is None:
        form_compiler_options = {}
    if jit_options is None:
        jit_options = {}
    if petsc_options is None:
        petsc_options = {}
    if newton_options is None:
        newton_options = {}
    if nitsche_parameters is None:
        nitsche_parameters = {}
    if physical_parameters is None:
        physical_parameters = {}

    # Compute lame parameters
    plane_strain = physical_parameters.get("strain", False)
    E = physical_parameters.get("E", 1e3)
    nu = physical_parameters.get("nu", 0.1)
    mu_func, lambda_func = lame_parameters(plane_strain)
    mu = mu_func(E, nu)
    lmbda = lambda_func(E, nu)
    sigma = sigma_func(mu, lmbda)

    # Nitsche parameters and variables
    theta = nitsche_parameters.get("theta", 1)
    gamma = nitsche_parameters.get("gamma", 10)

    # Unpack mesh data
    (facet_marker, dirichlet_value_elastic, contact_value_elastic, contact_value_rigid,
     dirichlet_value_rigid) = mesh_data
    assert facet_marker.dim == mesh.topology.dim - 1
    gdim = mesh.geometry.dim

    # Setup function space and functions used in Jacobian and residual formulation
    V = _fem.VectorFunctionSpace(mesh, ("CG", 1))
    u = _fem.Function(V)
    du = ufl.TrialFunction(V)
    u = _fem.Function(V)
    v = ufl.TestFunction(V)

    # Compute classical (volume) contributions of the equations of linear elasticity
    dx = ufl.Measure("dx", domain=mesh)
    J = ufl.inner(sigma(du), epsilon(v)) * dx
    F = ufl.inner(sigma(u), epsilon(v)) * dx

    # Nitsche for Dirichlet, another theta-scheme.
    # https://doi.org/10.1016/j.cma.2018.05.024
    if nitsche_bc:
        ds = ufl.Measure("ds", domain=mesh, subdomain_data=facet_marker)
        h = ufl.CellDiameter(mesh)
        n = ufl.FacetNormal(mesh)

        disp_vec = np.zeros(gdim)
        disp_vec[gdim - 1] = vertical_displacement
        u_D = ufl.as_vector(disp_vec)
        F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_elastic)\
             - theta * ufl.inner(sigma(v) * n, u - u_D) * \
            ds(dirichlet_value_elastic) + E * gamma / h * ufl.inner(u - u_D, v) * ds(dirichlet_value_elastic)

        J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_elastic)\
            - theta * ufl.inner(sigma(v) * n, du) * \
            ds(dirichlet_value_elastic) + E * gamma / h * ufl.inner(du, v) * ds(dirichlet_value_elastic)

        # Nitsche bc for rigid plane
        disp_plane = np.zeros(gdim)
        u_D_plane = ufl.as_vector(disp_plane)
        F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_rigid)\
             - theta * ufl.inner(sigma(v) * n, u - u_D_plane) * \
            ds(dirichlet_value_rigid) + E * gamma / h * ufl.inner(u - u_D_plane, v) * ds(dirichlet_value_rigid)
        J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_rigid)\
            - theta * ufl.inner(sigma(v) * n, du) * \
            ds(dirichlet_value_rigid) + E * gamma / h * ufl.inner(du, v) * ds(dirichlet_value_rigid)
    else:
        print("Dirichlet bc not implemented in custom assemblers yet.")

    # Custom assembly of contact boundary conditions
    _log.set_log_level(_log.LogLevel.OFF)  # avoid large amounts of output
    q_rule = dolfinx_contact.QuadratureRule(mesh.topology.cell_types[0], quadrature_degree,
                                            mesh.topology.dim - 1, basix.QuadratureType.Default)
    consts = np.array([gamma * E, theta])

    # Compute coefficients for mu and lambda as DG-0 functions
    V2 = _fem.FunctionSpace(mesh, ("DG", 0))
    lmbda2 = _fem.Function(V2)
    lmbda2.interpolate(lambda x: np.full((1, x.shape[1]), lmbda))
    mu2 = _fem.Function(V2)
    mu2.interpolate(lambda x: np.full((1, x.shape[1]), mu))

    # Compute integral entities on exterior facets (cell_index, local_index)
    contact_facets = facet_marker.find(contact_value_elastic)
    integral = _fem.IntegralType.exterior_facet
    integral_entities, num_local = dolfinx_contact.compute_active_entities(mesh._cpp_object, contact_facets, integral)
    integral_entities = integral_entities[:num_local, :]

    # Pack mu and lambda on facets
    coeffs = np.hstack([dolfinx_contact.cpp.pack_coefficient_quadrature(
        mu2._cpp_object, 0, integral_entities),
        dolfinx_contact.cpp.pack_coefficient_quadrature(
        lmbda2._cpp_object, 0, integral_entities)])
    # Pack celldiameter on facets
    surface_cells = np.unique(integral_entities[:, 0])
    h_int = _fem.Function(V2)
    expr = _fem.Expression(h, V2.element.interpolation_points())
    h_int.interpolate(expr, surface_cells)
    h_facets = dolfinx_contact.cpp.pack_coefficient_quadrature(
        h_int._cpp_object, 0, integral_entities)

    # Create contact class
    data = np.array([contact_value_elastic, contact_value_rigid], dtype=np.int32)
    offsets = np.array([0, 2], dtype=np.int32)
    surfaces = create_adjacencylist(data, offsets)
    contact = dolfinx_contact.cpp.Contact([facet_marker._cpp_object], surfaces, [(0, 1)],
                                          V._cpp_object, quadrature_degree=quadrature_degree)

    # Compute gap and normals
    contact.create_distance_map(0)
    g_vec = contact.pack_gap(0)
    n_surf = contact.pack_ny(0)

    # Create RHS kernels
    F_custom = _fem.form(F, jit_options=jit_options, form_compiler_options=form_compiler_options)
    kernel_rhs = dolfinx_contact.cpp.generate_contact_kernel(V._cpp_object, kt.Rhs, q_rule, False)

    # Create Jacobian kernels
    J_custom = _fem.form(J, jit_options=jit_options, form_compiler_options=form_compiler_options)
    kernel_J = dolfinx_contact.cpp.generate_contact_kernel(
        V._cpp_object, kt.Jac, q_rule, False)

    # NOTE: HACK to make "one-sided" contact work with assemble_matrix/assemble_vector
    contact_assembler = dolfinx_contact.cpp.Contact(
        [facet_marker._cpp_object], surfaces, [(0, 1)], V._cpp_object, quadrature_degree=quadrature_degree)

    # Pack coefficients to get numpy array of correct size for Newton solver
    u_packed = dolfinx_contact.cpp.pack_coefficient_quadrature(u._cpp_object, quadrature_degree, integral_entities)
    grad_u_packed = dolfinx_contact.cpp.pack_gradient_quadrature(u._cpp_object, quadrature_degree, integral_entities)

    offset = coeffs.shape[1] + h_facets.shape[1] + g_vec.shape[1]

    def pack_coefficients(x, solver_coeffs):
        """
        Function for updating pack coefficients inside the Newton solver.
        As only u is varying withing the Newton solver, we only update it.
        """
        u.vector[:] = x.array
        u_packed = dolfinx_contact.cpp.pack_coefficient_quadrature(u._cpp_object, quadrature_degree, integral_entities)
        grad_u_packed = dolfinx_contact.cpp.pack_gradient_quadrature(
            u._cpp_object, quadrature_degree, integral_entities)
        solver_coeffs[0][:, offset:offset + u_packed.shape[1]] = u_packed
        solver_coeffs[0][:, offset + u_packed.shape[1]:offset + u_packed.shape[1]
                         + grad_u_packed.shape[1]] = grad_u_packed

    def compute_residual(x, b, coeffs):
        """
        Compute residual for Newton solver RHS, given precomputed coefficients
        """
        with b.localForm() as b_local:
            b_local.set(0.0)
        contact_assembler.assemble_vector(b, 0, kernel_rhs, coeffs[0], consts)
        _fem.petsc.assemble_vector(b, F_custom)

    def compute_jacobian(x, A, coeffs):
        """
        Compute Jacobian for Newton solver LHS, given precomputed coefficients
        """
        A.zeroEntries()
        contact_assembler.assemble_matrix(A, [], 0, kernel_J, coeffs[0], consts)
        _fem.petsc.assemble_matrix(A, J_custom)
        A.assemble()

    # Setup non-linear problem and Newton-solver
    A = _fem.petsc.create_matrix(J_custom)
    b = _fem.petsc.create_vector(F_custom)

    coefficients = np.hstack([coeffs, h_facets, g_vec, u_packed, grad_u_packed, n_surf])
    solver = dolfinx_contact.NewtonSolver(mesh.comm, A, b, [coefficients])
    solver.set_jacobian(compute_jacobian)
    solver.set_residual(compute_residual)
    solver.set_coefficients(pack_coefficients)
    solver.set_krylov_options(petsc_options)

    # Create rigid motion null-space
    null_space = rigid_motions_nullspace(V)
    solver.A.setNearNullSpace(null_space)

    # Set Newton solver options

    # Create rigid motion null - space
    null_space = rigid_motions_nullspace(V)
    solver.A.setNearNullSpace(null_space)

    # Set Newton solver options
    solver.set_newton_options(newton_options)

    # Set initial condition
    def _u_initial(x):
        values = np.zeros((gdim, x.shape[1]))
        values[-1] = -vertical_displacement
        return values
    u.interpolate(_u_initial)

    dofs_global = V.dofmap.index_map_bs * V.dofmap.index_map.size_global
    _log.set_log_level(_log.LogLevel.INFO)

    # Solve non-linear problem
    with _common.Timer(f"{dofs_global} Solve Nitsche"):
        n, converged = solver.solve(u)
    u.x.scatter_forward()

    if solver.error_on_nonconvergence:
        assert converged
    print(f"{dofs_global}, Number of interations: {n:d}")

    return u
