// Copyright (C) 2021 Jørgen S. Dokken and Sarah Roggendorf
//
// This file is part of DOLFINx_CONTACT
//
// SPDX-License-Identifier:    MIT

#pragma once

#include <basix/cell.h>
#include <basix/finite-element.h>
#include <basix/quadrature.h>
#include <dolfinx/common/IndexMap.h>
#include <dolfinx/common/sort.h>
#include <dolfinx/fem/DofMap.h>
#include <dolfinx/fem/FiniteElement.h>
#include <dolfinx/fem/Function.h>
#include <dolfinx/fem/petsc.h>
#include <dolfinx/fem/utils.h>
#include <dolfinx/mesh/Mesh.h>
#include <dolfinx_cuas/QuadratureRule.hpp>
#include <xtensor/xtensor.hpp>
namespace dolfinx_contact
{
enum class Kernel
{
  Rhs,
  Jac,
  Rhs_variable_gap,
  Jac_variable_gap
};

/// This function computes the pull back for a set of points x on a cell
/// described by coordinate_dofs as well as the corresponding Jacobian, their
/// inverses and their determinants
/// @param[in, out] J: Jacobians of transformation from reference element to
/// physical element. Shape = (num_points, tdim, gdim). Computed at each point
/// in x
/// @param[in, out] K: inverse of J at each point.
/// @param[in, out] detJ: determinant of J at each  point
/// @param[in] x: points on physical element
/// @param[in ,out] X: pull pack of x (points on reference element)
/// @param[in] coordinate_dofs: geometry coordinates of cell
/// @param[in] cmap: the coordinate element
//-----------------------------------------------------------------------------
void pull_back(xt::xtensor<double, 3>& J, xt::xtensor<double, 3>& K,
               xt::xtensor<double, 1>& detJ, const xt::xtensor<double, 2>& x,
               xt::xtensor<double, 2>& X,
               const xt::xtensor<double, 2>& coordinate_dofs,
               const dolfinx::fem::CoordinateElement& cmap);

/// This function computes the pull back for a set of points x on a cell
/// described by coordinate_dofs as well as the corresponding Jacobian and
/// hessian.
/// FIXME: This is very similar to pull_back except second derivatives are
/// computed. as well. The two should probably be combined. For testing purposes
/// we will keep it separately for now.
/// @param[in, out] J: Jacobians of transformation from reference element to
/// physical element. Shape = (num_points, tdim, gdim). Computed at each point
/// in x
/// @param[in, out] H: Hessian at each point.
/// @param[in, out] K: inverse of J at each point.
/// @param[in] x: points on physical element
/// @param[in ,out] X: pull pack of x (points on reference element)
/// @param[in] coordinate_dofs: geometry coordinates of cell
/// @param[in] cmap: the coordinate element
//-----------------------------------------------------------------------------
void pull_back_2(xt::xtensor<double, 3>& J, xt::xtensor<double, 3>& K,
                 xt::xtensor<double, 3>& H, const xt::xtensor<double, 2>& x,
                 xt::xtensor<double, 2>& X,
                 const xt::xtensor<double, 2>& coordinate_dofs,
                 const dolfinx::fem::CoordinateElement& cmap);

//-----------------------------------------------------------------------------
/// This function computes the basis function values on a given cell at a
/// given set of points
/// @param[in, out] J: Jacobians of transformation from reference element to
/// physical element. Shape = (num_points, tdim, gdim). Computed at each point
/// in x
/// @param[in, out] K: inverse of J at each point.
/// @param[in, out] detJ: determinant of J at each  point
/// @param[in] x: points on physical element
/// @param[in] coordinate_dofs: geometry coordinates of cell
/// @param[in] index: the index of the cell (local to process)
/// @param[in] perm: permutation infor for cell
/// @param[in] element: the corresponding finite element
/// @param[in] cmap: the coordinate element
xt::xtensor<double, 4>
get_basis_functions(xt::xtensor<double, 3>& J, xt::xtensor<double, 3>& K,
                    xt::xtensor<double, 1>& detJ,
                    const xt::xtensor<double, 2>& x,
                    const xt::xtensor<double, 2>& coordinate_dofs,
                    const std::int32_t index, const std::int32_t perm,
                    std::shared_ptr<const dolfinx::fem::FiniteElement> element,
                    const dolfinx::fem::CoordinateElement& cmap,
                    const std::size_t num_derivatives);

/// @param[in] cells: the cells to be sorted
/// @param[in, out] perm: the permutation for the sorted cells
/// @param[out] pair(unique_cells, offsets): unique_cells is a vector of
/// sorted cells with all duplicates deleted, offsets contains the start and
/// end for each unique value in the sorted vector with all duplicates
// Example: cells = [5, 7, 6, 5]
//          unique_cells = [5, 6, 7]
//          offsets = [0, 2, 3, 4]
//          perm = [0, 3, 2, 1]
// Then given a cell and its index ("i") in unique_cells, one can recover the
// indices for its occurance in cells with perm[k], where
// offsets[i]<=k<offsets[i+1]. In the example if i = 0, then perm[k] = 0 or
// perm[k] = 3.
std::pair<std::vector<std::int32_t>, std::vector<std::int32_t>>
sort_cells(const xtl::span<const std::int32_t>& cells,
           const xtl::span<std::int32_t>& perm);

/// @param[in] u: dolfinx function on function space base on basix element
/// @param[in] mesh: mesh to be updated
/// Adds perturbation u to mesh
void update_geometry(const dolfinx::fem::Function<PetscScalar>& u,
                     std::shared_ptr<dolfinx::mesh::Mesh> mesh);

/// Compute the positive restriction of a double, i.e. f(x)= x if x>0 else 0
double R_plus(double x);

/// Compute the derivative of the positive restriction (i.e.) the step function.
/// @note Evaluates to 0 at x=0
double dR_plus(double x);

/// Compute the negative restriction of a double, i.e. f(x)= x if x < 0 else 0
double R_minus(double x);

/// Compute the derivative of the negative restriction (i.e.) the step function.
/// @note Evaluates to 0 at x=0
double dR_minus(double x);
} // namespace dolfinx_contact