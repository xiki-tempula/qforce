import scipy.optimize as optimize
# import scipy.linalg as la
# import scipy.optimize.nnls as nnls
import numpy as np
from .read_qm_out import QM
from .read_forcefield import Forcefield
from .write_forcefield import write_ff
from .dihedral_scan import scan_dihedral
from .molecule import Molecule
from .fragment import fragment
# , calc_g96angles
from .elements import elements
from .frequencies import calc_qm_vs_md_frequencies
# from .decorators import timeit, print_timelog


def fit_forcefield(inp, qm=None, mol=None):
    """
    Scope:
    ------
    Fit MD hessian to the QM hessian.

    TO DO:
    ------
    - Move calc_energy_forces to forces and clean it up
    - Include LJ, Coulomb flex dihed forces in the fitting as numbers

    CHECK
    -----
    - Does having (0,inf) fitting bound cause problems? metpyrl lower accuracy
      for dihed! Having -inf, inf causes problems for PTEG-1 (super high FKs)
    - Fix acetone angle! bond-angle coupling?)
    - Charges from IR intensities - together with interacting polarizable FF?
    """

    qm = QM(inp, "freq", fchk_file=inp.fchk_file, out_file=inp.qm_freq_out)

    mol = Molecule(inp, qm)

    fit_results, md_hessian = fit_hessian(inp, mol, qm, ignore_flex=True)
    average_unique_minima(mol.terms)

    if inp.fragment:
        fragment(inp, mol, qm)

    calc_qm_vs_md_frequencies(inp, qm, md_hessian)
    make_ff_params_from_fit(mol.terms, mol.topo, fit_results, inp, qm)

    # temporary
    # fit_dihedrals(inp, mol, qm)


def fit_hessian(inp, mol, qm, ignore_flex=True):
    hessian, full_md_hessian_1d = [], []
    non_fit = []
    qm_hessian = np.copy(qm.hessian)

    print("Calculating the MD hessian matrix elements...")
    full_md_hessian = calc_hessian(qm.coords, mol, inp, ignore_flex)

    count = 0
    print("Fitting the MD hessian parameters to QM hessian values")
    for i in range(mol.topo.n_atoms*3):
        for j in range(i+1):
            hes = (full_md_hessian[i, j] + full_md_hessian[j, i]) / 2
            if all([h == 0 for h in hes]) or np.abs(qm_hessian[count]) < 1e+1:
                qm_hessian = np.delete(qm_hessian, count)
                full_md_hessian_1d.append(np.zeros(mol.terms.n_fitted_terms))
            else:
                count += 1
                hessian.append(hes[:-1])
                full_md_hessian_1d.append(hes[:-1])
                non_fit.append(hes[-1])

    difference = qm_hessian - np.array(non_fit)
    # la.lstsq or nnls could also be used:
    fit = optimize.lsq_linear(hessian, difference, bounds=(0, np.inf)).x
    print("Done!\n")

    for term in mol.terms:
        if term.idx < len(fit):
            term.fconst = fit[term.idx]

    full_md_hessian_1d = np.sum(full_md_hessian_1d * fit, axis=1)

    return fit, full_md_hessian_1d


def fit_dihedrals(inp, mol, qm):
    """
    Temporary - to be removed
    """
    from .fragment import check_one_fragment
    for term in mol.terms['dihedral/flexible']:
        frag_name, _, _, _, _, _ = check_one_fragment(inp, mol, qm, term.atomids)
        scan_dihedral(inp, term.atomids, frag_name)


def calc_hessian(coords, mol, inp, ignore_flex):
    """
    Scope:
    -----
    Perform displacements to calculate the MD hessian numerically.
    """
    full_hessian = np.zeros((3*mol.topo.n_atoms, 3*mol.topo.n_atoms,
                             mol.terms.n_fitted_terms+1))

    for a in range(mol.topo.n_atoms):
        for xyz in range(3):
            coords[a][xyz] += 0.003
            f_plus = calc_forces(coords, mol, inp, ignore_flex)
            coords[a][xyz] -= 0.006
            f_minus = calc_forces(coords, mol, inp, ignore_flex)
            coords[a][xyz] += 0.003
            diff = - (f_plus - f_minus) / 0.006
            full_hessian[a*3+xyz] = diff.reshape(mol.terms.n_fitted_terms+1, 3*mol.topo.n_atoms).T
    return full_hessian


def calc_forces(coords, mol, inp, ignore_flex):
    """
    Scope:
    ------
    For each displacement, calculate the forces from all terms.

    """
    if ignore_flex:
        ignores = ['dihedral/flexible', 'dihedral/constr']
    else:
        ignores = []

    force = np.zeros((mol.terms.n_fitted_terms+1, mol.topo.n_atoms, 3))

    with mol.terms.add_ignore(ignores):
        for term in mol.terms:
            term.do_fitting(coords, force)

    return force


def average_unique_minima(terms):
    unique_terms = {}
    averaged_terms = ['bond', 'angle']
    for name in [term_name for term_name in averaged_terms if term_name in terms.term_names]:
        for term in terms[name]:
            if str(term) in unique_terms.keys():
                term.equ = unique_terms[str(term)]
            else:
                eq = np.where(np.array(list(oterm.idx for oterm in terms[name])) == term.idx)
                minimum = np.array(list(oterm.equ for oterm in terms[name]))[eq].mean()
                term.equ = minimum
                unique_terms[str(term)] = minimum

    # For Urey, recalculate length based on the averaged bonds/angles
    for term in terms['urey']:
        if str(term) in unique_terms.keys():
            term.equ = unique_terms[str(term)]
        else:
            bond1_atoms = sorted(term.atomids[:2])
            bond2_atoms = sorted(term.atomids[1:])
            bond1 = [bond.equ for bond in terms['bond'] if all(bond1_atoms == bond.atomids)][0]
            bond2 = [bond.equ for bond in terms['bond'] if all(bond2_atoms == bond.atomids)][0]
            angle = [ang.equ for ang in terms['angle'] if all(term.atomids == ang.atomids)][0]
            urey = (bond1**2 + bond2**2 - 2*bond1*bond2*np.cos(angle))**0.5
            term.equ = urey
            unique_terms[str(term)] = urey


def make_ff_params_from_fit(terms, topo, fit, inp, qm, polar=False):
    """
    Scope:
    -----
    Convert units, average over equivalent minima and prepare everything
    to be written as a forcefield file.
    """
    ff = Forcefield()
    e = elements()
    bohr2nm = 0.052917721067
    ff.mol_type = inp.job_name
    ff.natom = topo.n_atoms
    ff.box = [10., 10., 10.]
    ff.n_mol = 1
    ff.coords = list(qm.coords/10)
    ff.exclu = [[] for _ in range(topo.n_atoms)]
    masses = [round(e.mass[i], 5) for i in qm.atomids]
    atom_no = range(1, topo.n_atoms + 1)
    atom_names = []
    atom_dict = {}

    for i, a in enumerate(qm.atomids):
        sym = e.sym[a]
        if sym not in atom_dict:
            atom_dict[sym] = 1
        else:
            atom_dict[sym] += 1
        atom_names.append(f'{sym}{atom_dict[sym]}')

    for lj_type, lj_params in topo.lj_type_dict.items():
        ff.atom_types.append([lj_type, 0, 0, "A", lj_params[0], lj_params[1]])

    for n, lj_type, atom_name, q, mass in zip(atom_no, topo.lj_types, atom_names, topo.q, masses):
        ff.atoms.append([n, lj_type, 1, "MOL", atom_name, n, q, mass])

    for exclusion in topo.exclusions:
        ff.exclu[exclusion[0]].append(exclusion[1]+1)

    # if polar:
    #     alphas = qm.alpha*bohr2nm**3
    #     drude = {}
    #     n_drude = 1
    #     ff.atom_types.append(["DP", 0, 0, "S", 0, 0])

    #     for i, alpha in enumerate(alphas):
    #         if alpha > 0:
    #             drude[i] = mol.topo.n_atoms+n_drude
    #             ff.atoms[i][6] += 8
    #             # drude atoms
    #             ff.atoms.append([drude[i], 'DP', 2, 'MOL', f'D{atoms[i]}',
    #                              i+1, -8., 0.])
    #             ff.coords.append(ff.coords[i])
    #             # polarizability
    #             ff.polar.append([i+1, drude[i], 1, alpha])
    #             n_drude += 1
    #     ff.natom = len(ff.atoms)
    #     for i, alpha in enumerate(alphas):
    #         if alpha > 0:
    #             # exclusions for balancing the drude particles
    #             for j in mol.topo.neighbors[inp.nrexcl-2][i]+mol.topo.neighbors[inp.nrexcl-1][i]:
    #                 if alphas[j] > 0:
    #                     ff.exclu[drude[i]-1].extend([drude[j]])
    #             for j in mol.topo.neighbors[inp.nrexcl-1][i]:
    #                 ff.exclu[drude[i]-1].extend([j+1])
    #             ff.exclu[drude[i]-1].sort()
    #             # thole polarizability
    #             for neigh in [mol.topo.neighbors[n][i] for n in range(inp.nrexcl)]:
    #                 for j in neigh:
    #                     if i < j and alphas[j] > 0:
    #                         ff.thole.append([i+1, drude[i], j+1, drude[j], "2", 2.6, alpha,
    #                                          alphas[j]])

    for term in terms['bond']:
        atoms = [a+1 for a in term.atomids]
        ff.bonds.append(atoms + [1, term.equ*0.1, term.fconst*100])

    for term in terms['angle']:
        atoms = [a+1 for a in term.atomids]
        ff.angles.append(atoms + [1, np.degrees(term.equ), term.fconst])

    if inp.urey:
        angle_atoms = np.array(ff.angles)[:, :3]
        for term in terms['urey']:
            match = np.all(term.atomids+1 == angle_atoms, axis=1)
            match = np.nonzero(match)[0][0]
            ff.angles[match][3] = 5
            ff.angles[match].extend([term.equ*0.1, term.fconst*100])

    for term in terms['dihedral/rigid']:
        atoms = [a+1 for a in term.atomids]
        minimum = np.degrees(term.equ)
        ff.dihedrals.append(atoms + [2, minimum, term.fconst])

    for term in terms['dihedral/improper']:
        atoms = [a+1 for a in term.atomids]
        minimum = np.degrees(term.equ)
        ff.impropers.append(atoms + [2, minimum, term.fconst])

    uniques = list(set(str(term) for term in terms['dihedral/flexible']))
    for term in terms['dihedral/flexible']:
        atoms = [a+1 for a in term.atomids]
        if inp.fragment:
            ff.flexible.append(atoms + [3] + list(term.equ))
        else:
            ff.flexible.append(atoms + [3, uniques.index(str(term))+1])

    uniques = list(set(str(term) for term in terms['dihedral/constr']))
    for term in terms['dihedral/constr']:
        atoms = [a+1 for a in term.atomids]
        ff.constrained.append(atoms + [3, uniques.index(str(term))+1])

    write_ff(ff, inp, polar)
    print("Q-Force force field parameters (.itp, .top) can be found in the "
          f"directory: {inp.job_dir}/")
