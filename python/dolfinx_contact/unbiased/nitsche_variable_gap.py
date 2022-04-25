# Copyright (C) 2021 Sarah Roggendorf
#
# SPDX-License-Identifier:    MIT

from typing import Tuple, Union

import dolfinx.common as _common
import dolfinx.fem as _fem
import dolfinx.log as _log
import dolfinx.mesh as _mesh
import dolfinx_cuas
import numpy as np
import ufl
from dolfinx.io import XDMFFile
from petsc4py.PETSc import Viewer

import dolfinx_contact
import dolfinx_contact.cpp
from dolfinx_contact.helpers import epsilon, lame_parameters, sigma_func, rigid_motions_nullspace

kt = dolfinx_contact.cpp.Kernel

__all__ = ["nitsche_variable_gap"]


def nitsche_variable_gap(mesh: _mesh.Mesh, mesh_data: Tuple[_mesh.MeshTagsMetaClass, int, int, int, int],
                         physical_parameters: dict[str, Union[bool, np.float64, int]],
                         nitsche_parameters: dict[str, np.float64],
                         displacement: Tuple[list[float], list[float]] = [[0, 0, 0], [0, 0, 0]],
                         quadrature_degree: int = 5, form_compiler_params: dict = None, jit_params: dict = None,
                         petsc_options: dict = None, newton_options: dict = None, initGuess=None, outfile=None):
    """
    Use custom kernel to compute the contact problem with two elastic bodies coming into contact.
    The gap function is non-linear in u and based on closest point projection on the deformed body.

    Parameters
    ==========
    mesh
        The input mesh
    mesh_data
        A quinteplet with a mesh tag for facets and values v0, v1, v2, v3. v0
        and v3 should be the values in the mesh tags for facets to apply a Dirichlet
        condition on, where v0 corresponds to the first elastic body and v3 to the second.
        v1 is the value for facets on the first body that is in the potential contact zone.
        v2 is the value for facets on the second body in potential contact zone
    physical_parameters
        Optional dictionary with information about the linear elasticity problem.
        Valid (key, value) tuples are: ('E': float), ('nu', float), ('strain', bool)
    nitsche_parameters
        Optional dictionary with information about the Nitsche configuration.
        Valid (keu, value) tuples are: ('gamma', float), ('theta', float) where theta can be -1, 0 or 1 for
        skew-symmetric, penalty like or symmetric enforcement of Nitsche conditions
    displacement
        The displacement enforced on Dirichlet boundary
    quadrature_degree
        The quadrature degree to use for the custom contact kernels
    form_compiler_params
        Parameters used in FFCX compilation of this form. Run `ffcx --help` at
        the commandline to see all available options. Takes priority over all
        other parameter values, except for `scalar_type` which is determined by
        DOLFINX.
    jit_params
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
    outfile
        File to append solver summary
    """
    form_compiler_params = {} if form_compiler_params is None else form_compiler_params
    jit_params = {} if jit_params is None else jit_params
    petsc_options = {} if petsc_options is None else petsc_options
    newton_options = {} if newton_options is None else newton_options

    if physical_parameters.get("strain") is None:
        raise RuntimeError("Need to supply if problem is plane strain (True) or plane stress (False)")
    else:
        plane_strain = physical_parameters.get("strain")
    if physical_parameters.get("E") is None:
        raise RuntimeError("Need to supply Youngs modulus")
    else:
        E = physical_parameters.get("E")
    if physical_parameters.get("nu") is None:
        raise RuntimeError("Need to supply Poisson's ratio")
    else:
        nu = physical_parameters.get("nu")

    # Compute lame parameters
    mu_func, lambda_func = lame_parameters(plane_strain)
    mu = mu_func(E, nu)
    lmbda = lambda_func(E, nu)
    sigma = sigma_func(mu, lmbda)

    # Nitche parameters and variables
    if nitsche_parameters.get("theta") is None:
        raise RuntimeError("Need to supply theta for Nitsche imposition of boundary conditions")
    else:
        theta = nitsche_parameters.get("theta")
    if nitsche_parameters.get("gamma") is None:
        raise RuntimeError("Need to supply Coercivity/Stabilization parameter for Nitsche condition")
    else:
        gamma = E * nitsche_parameters.get("gamma")

    # Unpack mesh data
    (facet_marker, dirichlet_value_0, surface_value_0, surface_value_1, dirichlet_value_1) = mesh_data
    assert(facet_marker.dim == mesh.topology.dim - 1)

    # Functions space and FEM functions
    V = _fem.VectorFunctionSpace(mesh, ("CG", 1))
    gdim = mesh.geometry.dim
    u = _fem.Function(V)
    v = ufl.TestFunction(V)
    du = ufl.TrialFunction(V)

    h = ufl.CellDiameter(mesh)
    n = ufl.FacetNormal(mesh)
    # Integration measure and ufl part of linear/bilinear form
    metadata = {"quadrature_degree": quadrature_degree}
    dx = ufl.Measure("dx", domain=mesh)
    ds = ufl.Measure("ds", domain=mesh, metadata=metadata,
                     subdomain_data=facet_marker)
    J = ufl.inner(sigma(du), epsilon(v)) * dx - 0.5 * theta * h / gamma * ufl.inner(sigma(du) * n, sigma(v) * n) * \
        ds(surface_value_0) - 0.5 * theta * h / gamma * ufl.inner(sigma(du) * n, sigma(v) * n) * ds(surface_value_1)
    F = ufl.inner(sigma(u), epsilon(v)) * dx - 0.5 * theta * h / gamma * ufl.inner(sigma(u) * n, sigma(v) * n) * \
        ds(surface_value_0) - 0.5 * theta * h / gamma * ufl.inner(sigma(u) * n, sigma(v) * n) * ds(surface_value_1)

    # Nitsche for Dirichlet, another theta-scheme.
    # https://doi.org/10.1016/j.cma.2018.05.024
    # Nitsche bc for body 0
    disp_0 = displacement[0][:gdim]
    u_D_0 = ufl.as_vector(disp_0)
    F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_0)\
        - theta * ufl.inner(sigma(v) * n, u - u_D_0) * \
        ds(dirichlet_value_0) + gamma / h * ufl.inner(u - u_D_0, v) * ds(dirichlet_value_0)

    J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_0)\
        - theta * ufl.inner(sigma(v) * n, du) * \
        ds(dirichlet_value_0) + gamma / h * ufl.inner(du, v) * ds(dirichlet_value_0)
    # Nitsche bc for body 1
    disp_1 = displacement[1][:gdim]
    u_D_1 = ufl.as_vector(disp_1)
    F += - ufl.inner(sigma(u) * n, v) * ds(dirichlet_value_1)\
        - theta * ufl.inner(sigma(v) * n, u - u_D_1) * \
        ds(dirichlet_value_1) + gamma / h * ufl.inner(u - u_D_1, v) * ds(dirichlet_value_1)
    J += - ufl.inner(sigma(du) * n, v) * ds(dirichlet_value_1)\
        - theta * ufl.inner(sigma(v) * n, du) * \
        ds(dirichlet_value_1) + gamma / h * ufl.inner(du, v) * ds(dirichlet_value_1)

    # Custom assembly
    # create contact class
    with _common.Timer("~Contact: Init"):
        contact = dolfinx_contact.cpp.Contact(facet_marker, [surface_value_0, surface_value_1], V._cpp_object)
    contact.set_quadrature_degree(quadrature_degree)
    with _common.Timer("~Contact: Distance maps"):
        contact.create_distance_map(0, 1)
        contact.create_distance_map(1, 0)
    # pack constants
    consts = np.array([gamma, theta])

    # Pack material parameters mu and lambda on each contact surface
    with _common.Timer("~Contact: Interpolate coeffs (mu, lmbda)"):
        V2 = _fem.FunctionSpace(mesh, ("DG", 0))
        lmbda2 = _fem.Function(V2)
        lmbda2.interpolate(lambda x: np.full((1, x.shape[1]), lmbda))
        mu2 = _fem.Function(V2)
        mu2.interpolate(lambda x: np.full((1, x.shape[1]), mu))

    with _common.Timer("~Contact: Compute active entities"):
        facets_0 = facet_marker.indices[facet_marker.values == surface_value_0]
        facets_1 = facet_marker.indices[facet_marker.values == surface_value_1]
        integral = _fem.IntegralType.exterior_facet
        entities_0 = dolfinx_cuas.compute_active_entities(mesh, facets_0, integral)
        entities_1 = dolfinx_cuas.compute_active_entities(mesh, facets_1, integral)

    with _common.Timer("~Contact: Pack coeffs (mu, lmbda"):
        material_0 = dolfinx_cuas.pack_coefficients([mu2, lmbda2], entities_0)
        material_1 = dolfinx_cuas.pack_coefficients([mu2, lmbda2], entities_1)

    # Pack celldiameter on each surface
    with _common.Timer("~Contact: Compute and pack celldiameter"):
        surface_cells = np.unique(np.hstack([entities_0[:, 0], entities_1[:, 0]]))
        h_int = _fem.Function(V2)
        expr = _fem.Expression(h, V2.element.interpolation_points)
        h_int.interpolate(expr, surface_cells)
        h_0 = dolfinx_cuas.pack_coefficients([h_int], entities_0)
        h_1 = dolfinx_cuas.pack_coefficients([h_int], entities_1)

    # Concatenate all coeffs
    coeff_0 = np.hstack([material_0, h_0])
    coeff_1 = np.hstack([material_1, h_1])

    # Generate Jacobian data structures
    J_cuas = _fem.form(J, form_compiler_params=form_compiler_params, jit_params=jit_params)
    with _common.Timer("~Contact: Generate Jacobian kernel"):
        kernel_jac = contact.generate_kernel(kt.Jac_variable_gap)
    with _common.Timer("~Contact: Create matrix"):
        J = contact.create_matrix(J_cuas)

    # Generate residual data structures
    F_cuas = _fem.form(F, form_compiler_params=form_compiler_params, jit_params=jit_params)
    with _common.Timer("~Contact: Generate residual kernel"):
        kernel_rhs = contact.generate_kernel(kt.Rhs_variable_gap)
    with _common.Timer("~Contact: Create vector"):
        b = _fem.petsc.create_vector(F_cuas)

    def update_mesh(u):
        with _common.Timer("Contact: Update submeshes"):
            contact.update_submesh_geometry(u._cpp_object)
        with _common.Timer("~Contact: Distance maps"):
            contact.create_distance_map(0, 1)
            contact.create_distance_map(1, 0)
        submesh_0 = contact.submesh(0)
        submesh_1 = contact.submesh(1)
        with XDMFFile(submesh_0.comm, "results/sumbesh_0.xdmf", "w") as xdmf:
            xdmf.write_mesh(submesh_0)
        with XDMFFile(submesh_1.comm, "results/sumbesh_1.xdmf", "w") as xdmf:
            xdmf.write_mesh(submesh_1)

    @_common.timed("~Contact: Update coefficients")
    def compute_coefficients(x, coeffs):
        _log.set_log_level(_log.LogLevel.OFF)
        u.vector[:] = x.array
        update_mesh(u)
        with _common.Timer("~~Contact: Pack gap"):
            gap_0 = contact.pack_gap(0)
            gap_1 = contact.pack_gap(1)
        with _common.Timer("~~Contact: Pack normals"):
            n_0 = contact.pack_ny(0, gap_0)
            n_1 = contact.pack_ny(1, gap_1)
        with _common.Timer("~~Contact: Pack test functions"):
            test_fn_0 = contact.pack_test_functions(0, gap_0, 1)
            test_fn_1 = contact.pack_test_functions(1, gap_1, 1)
        with _common.Timer("~~Contact: Pack u contact"):
            u_opp_0 = contact.pack_u_contact(0, u._cpp_object, gap_0, 1)
            u_opp_1 = contact.pack_u_contact(1, u._cpp_object, gap_1, 1)
        with _common.Timer("~~Contact: Pack u"):
            u_0 = dolfinx_cuas.pack_coefficients([u], entities_0)
            u_1 = dolfinx_cuas.pack_coefficients([u], entities_1)
        with _common.Timer("~~Contact: Pack transformation derivatives"):
            surf_der_0 = contact.pack_surface_derivatives(0, gap_0)
            surf_der_1 = contact.pack_surface_derivatives(1, gap_1)
        with _common.Timer("~~Contact: Pack indices of contact facets"):
            facet_ind_0 = contact.pack_facet_indices(0)
            facet_ind_1 = contact.pack_facet_indices(1)
        c_0 = np.hstack([coeff_0, gap_0, n_0, test_fn_0, u_0, u_opp_0, surf_der_0])
        c_1 = np.hstack([coeff_1, gap_1, n_1, test_fn_1, u_1, u_opp_1, surf_der_1])
        coeffs[0][:, :] = c_0[:, :]
        coeffs[1][:, :] = c_1[:, :]
        coeffs[2][:, :] = facet_ind_0[:, :]
        coeffs[3][:, :] = facet_ind_1[:, :]

    @_common.timed("~Contact: Assemble matrix")
    def compute_jacobian_matrix(x, A, coeffs):
        u.vector[:] = x.array
        A.zeroEntries()
        with _common.Timer("~~Contact: Contact contributions (in assemble matrix)"):
            contact.assemble_matrix(A, [], 0, kernel_jac, coeffs[0], consts, coeffs[2])
            contact.assemble_matrix(A, [], 1, kernel_jac, coeffs[1], consts, coeffs[3])
        with _common.Timer("~~Contact: Standard contributions (in assemble matrix)"):
            _fem.petsc.assemble_matrix(A, J_cuas)
        A.assemble()

    @_common.timed("~Contact: Assemble residual")
    def compute_residual(x, b, coeffs):
        b.zeroEntries()
        u.vector[:] = x.array
        with _common.Timer("~~Contact: Contact contributions (in assemble vector)"):
            contact.assemble_vector(b, 0, kernel_rhs, coeffs[0], consts)
            contact.assemble_vector(b, 1, kernel_rhs, coeffs[1], consts)
        with _common.Timer("~~Contact: Standard contributions (in assemble vector)"):
            _fem.petsc.assemble_vector(b, F_cuas)

    # coefficient arrays
    num_coeffs = contact.coefficients_size(variable_gap=True)
    num_q_points = contact.num_quadrature_points()
    coeffs = [np.zeros((facets_0.size, num_coeffs)), np.zeros((facets_1.size, num_coeffs)),
              np.zeros((facets_0.size, num_q_points), dtype=np.int32),
              np.zeros((facets_1.size, num_q_points), dtype=np.int32)]

    newton_solver = dolfinx_contact.NewtonSolver(mesh.comm, J, b, coeffs)

    # Set matrix-vector computations
    newton_solver.setF(compute_residual)
    newton_solver.setJ(compute_jacobian_matrix)
    newton_solver.setCoeffs(compute_coefficients)

    # Set rigid motion nullspace
    null_space = rigid_motions_nullspace(V)
    newton_solver.A.setNearNullSpace(null_space)

    # Set Newton solver options
    if newton_options.get("atol") is not None:
        newton_solver.atol = newton_options.get("atol")
    if newton_options.get("rtol") is not None:
        newton_solver.rtol = newton_options.get("rtol")

    if newton_options.get("convergence_criterion") is not None:
        conv_crit = newton_options.get("convergence_criterion")
        if conv_crit == "incremental":
            crit = dolfinx_contact.ConvergenceCriterion.incremental
        elif conv_crit == "residual":
            crit = dolfinx_contact.ConvergenceCriterion.residual
        else:
            raise RuntimeError(f"Unknown convergence criterion '{conv_crit}'")
        newton_solver.convergence_criterion = crit
    # TODO: restore options after debugging
    # if newton_options.get("max_it") is not None:
    #     newton_solver.max_it = newton_options.get("max_it")
    newton_solver.max_it = 10
    # if newton_options.get("error_on_nonconvergence") is not None:
    #     newton_solver.error_on_nonconvergence = newton_options.get("error_on_nonconvergence")
    newton_solver.error_on_nonconvergence = False
    if newton_options.get("relaxation_parameter") is not None:
        newton_solver.relaxation_parameter = newton_options.get("relaxation_parameter")

    # Set initial guess
    if initGuess is None:
        u.x.array[:] = 0
    else:
        u.x.array[:] = 0  # initGuess.x.array[:]

    # Set Krylov solver options
    # TODO: Restore petsc options from input after debugging
    petsc_options = {"ksp_type": "preonly", "pc_type": "lu"}
    newton_solver.set_krylov_options(petsc_options)

    dofs_global = V.dofmap.index_map_bs * V.dofmap.index_map.size_global
    _log.set_log_level(_log.LogLevel.OFF)

    # Solve non-linear problem
    timing_str = f"~Contact: {id(dofs_global)} Solve Nitsche"
    with _common.Timer(timing_str):
        n, converged = newton_solver.solve(u)

    if outfile is not None:
        viewer = Viewer().createASCII(outfile, "a")
        newton_solver.krylov_solver.view(viewer)
    newton_time = _common.timing(timing_str)
    # if not converged:
    #     raise RuntimeError("Newton solver did not converge")
    u.x.scatter_forward()

    print(f"{dofs_global}\n Number of Newton iterations: {n:d}\n",
          f"Number of Krylov iterations {newton_solver.krylov_iterations}\n", flush=True)
    return u, n, newton_solver.krylov_iterations, newton_time[1]