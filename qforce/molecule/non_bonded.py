import subprocess
import numpy as np
import pulp
from scipy.optimize import curve_fit
from itertools import combinations_with_replacement
#
from .. import qforce_data


class NonBonded():
    def __init__(self, inp, q, lj_type_dict, lj_types, lj_pairs, exclusions, n_excl,):
        self.q = q
        self.lj_type_dict = lj_type_dict  # in GROMACS UNITS
        self.lj_types = lj_types
        self.lj_pairs = lj_pairs
        self.exclusions = exclusions
        self.n_excl = n_excl
        # polar additions
        # self.polar_q = polar_q
        # self.polar_pairs = polar_pairs
        # self.polar_fcs = polar_fcs

    @classmethod
    def from_topology(cls, inp, qm, topo):
        # RUN D4 IF NECESSARY
        if 'd4' in [inp.point_charges, inp.lennard_jones]:
            q, lj_a, lj_b = handle_d4(inp, qm, topo)

        # CHARGES - esp and resp
        if inp.point_charges == 'ext':
            q = np.loadtxt(f'{inp.job_dir}/ext_q', comments=['#', ';'])
        elif inp.point_charges == 'cm5':
            q = qm.cm5
        q = average_equivalent_terms(topo, [q])[0]
        q = sum_charges_to_qtotal(topo, q)

        # LENNARD-JONES
        if inp.lennard_jones != 'd4':
            lj_types, lj_type_dict, lj_pairs = set_external_lennard_jones(inp)
        else:
            lj_types, lj_type_dict, lj_pairs = set_qforce_lennard_jones(topo, inp, lj_a, lj_b)
            print('WARNING: You are using Q-Force Lennard-Jones parameters. This is not finished.',
                  '\nYou are advised to provide external LJ parameters for production runs.\n')

        # polar_q, polar_pairs, polar_fcs = set_polar(q, topo, inp)

        return cls(inp, q, lj_type_dict, lj_types, lj_pairs, inp.exclusions, inp.n_excl)

    @classmethod
    def subset(cls, inp, non_bonded, mapping):
        n_atoms = len(mapping)
        rev_map = {v: k for k, v in mapping.items()}

        q = np.array([non_bonded.q[rev_map[i]] for i in range(n_atoms)])
        lj_types = [non_bonded.lj_types[rev_map[i]] for i in range(n_atoms)]
        lj_type_dict = {key: val for key, val in non_bonded.lj_type_dict.items()
                        if key in lj_types}
        lj_pairs = {key: val for key, val in list(non_bonded.lj_pairs.items())
                    if key[0] in lj_types and key[1] in lj_types}
        exclusions = [(mapping[excl[0]], mapping[excl[1]]) for excl in inp.exclusions if
                      excl[0] in mapping.keys() and excl[1] in mapping.keys()]

        return cls(inp, q, lj_type_dict, lj_types, lj_pairs, exclusions, inp.n_excl)


def set_polar(q, topo, inp):
    EPS0 = 1389.35458  # kJ*ang/mol/e2
    polar_dict = {1: 0.45330, 6: 1.30300, 7: 0.98840, 8: 0.83690, 16: 2.47400}
    polar_fcs = []
    polar_pairs = []

    polar_q = q + 8

    for q, elem in zip(q, topo.elements):
        polar_fcs.append(64.0 * EPS0 / polar_dict[elem])

    for i in range(topo.n_atoms):
        for j in range(i+1, topo.n_atoms):
            close_neighbor = any([j in topo.neighbors[c][i] for c in range(inp.n_excl)])
            if not close_neighbor and (i, j) not in inp.exclusions:
                polar_pairs.append([i, j])

    print(polar_q, polar_pairs, polar_fcs)

    return polar_q, polar_pairs, polar_fcs


def set_qforce_lennard_jones(topo, inp, lj_a, lj_b):
    lj_type_dict = {}
    lj_pairs = {}
    lj_types = topo.types
    for i, atype in enumerate(lj_types[topo.unique_atomids]):
        lj_type_dict[atype] = [lj_a[i], lj_b[i]]

    for comb in combinations_with_replacement(lj_type_dict.keys(), 2):
        comb = tuple(sorted(comb))
        params = use_combination_rule(lj_type_dict[comb[0]], lj_type_dict[comb[1]], inp)
        lj_pairs[comb] = get_c6_c12_for_diff_comb_rules(inp, params)
    return lj_types, lj_type_dict, lj_pairs


def set_external_lennard_jones(inp):
    lj_type_dict = {}
    lj_pairs = {}

    lj_types = np.loadtxt(f'{inp.job_dir}/ext_lj', dtype='str', comments=['#', ';'])
    atom_types, nonbond_params = read_ext_nonbonded_file(inp)

    for ext_type in set(lj_types):
        lj_type_dict[ext_type] = atom_types[ext_type]

    for comb in combinations_with_replacement(set(lj_types), 2):
        comb = tuple(sorted(comb))

        if comb in nonbond_params.keys():
            params = nonbond_params[comb]
        elif comb[0] == comb[1]:
            params = atom_types[comb[0]]
        else:
            params = use_combination_rule(atom_types[comb[0]], atom_types[comb[1]], inp)

        lj_pairs[comb] = get_c6_c12_for_diff_comb_rules(inp, params)

    return lj_types, lj_type_dict, lj_pairs


def get_c6_c12_for_diff_comb_rules(inp, params):
    if inp.comb_rule == 1:
        c6 = params[0] * 1e6
        c12 = params[1] * 1e12
    else:
        sigma = params[0] * 10
        epsilon = params[1]
        sigma6 = sigma**6
        c6 = 4 * epsilon * sigma6
        c12 = c6 * sigma6
    return [c6, c12]


def use_combination_rule(param1, param2, inp):
    b = (param1[1] * param2[1])**0.5
    if inp.comb_rule in [1, 3]:
        a = (param1[0] * param2[0])**0.5
    else:
        a = (param1[0] + param2[0]) / 2
    return a, b


def calc_sigma_epsilon(c6, c12):
    sigma = (c12/c6)**(1/6)
    epsilon = c6 / (4*sigma**6)
    return sigma, epsilon


def read_ext_nonbonded_file(inp):
    atom_types, nonbond_params = {}, {}

    if inp.lennard_jones == 'gromos':
        file_name = 'gromos_atb3_ffnonbonded.itp'
    elif inp.lennard_jones == 'opls':
        file_name = 'opls_ffnonbonded.itp'
    elif inp.lennard_jones == 'gaff':
        file_name = 'gaff_ffnonbonded.itp'

    with open(f'{qforce_data}/{file_name}', 'r') as file:
        for line in file:
            line = line.partition('#')[0].partition(';')[0].strip()
            if line == '':
                continue
            elif "[" in line and "]" in line:
                no_space = line.lower().replace(" ", "")
                in_section = no_space[no_space.index("[")+1:no_space.index("]")]
            else:
                line = line.split()
                if in_section == 'atomtypes':
                    atype, a, b = line[0], float(line[-2]), float(line[-1])
                    atom_types[atype] = [a, b]
                elif in_section == 'nonbond_params':
                    atype1, atype2, a, b = line[0], line[1], float(line[-2]), float(line[-1])
                    nonbond_params[tuple(sorted([atype1, atype2]))] = [a, b]

        return atom_types, nonbond_params


def average_equivalent_terms(topo, terms):
    avg_terms = []
    for term in terms:
        term = np.array(term)
        for l in topo.list:
            term[l] = term[l].mean().round(5)
        avg_terms.append(term)
    return avg_terms


def sum_charges_to_qtotal(topo, q):
    total = q.sum()
    q_integer = round(total)
    extra = int(100000 * round(total - q_integer, 5))
    if extra != 0:
        if extra > 0:
            sign = 1
        else:
            sign = -1
            extra = - extra

        n_eq = [len(l) for l in topo.list]
        eq_no = [f"{i:05d}" for i, _ in enumerate(n_eq)]

        var = pulp.LpVariable.dicts("x", eq_no, lowBound=0, cat='Integer')
        prob = pulp.LpProblem('prob', pulp.LpMinimize)
        prob += pulp.lpSum([var[n] for n in eq_no])
        prob += pulp.lpSum([eq * var[eq_no[i]] for i, eq in enumerate(n_eq)]) == extra
        prob.solve()

        if prob.status == 1:
            for i, v in enumerate(prob.variables()):
                q[topo.list[i]] -= sign * v.varValue / 100000
        else:
            print('Failed to equate total of charges to the total charge of '
                  'the system. Do so manually')
    return q


def handle_d4(inp, qm, topo):
    n_more = 0
    c6, c8, alpha, r_rel, q = [], [], [], [], []
    lj_a, lj_b = None, None

    d4_out = run_d4(inp)

    with open(f'{inp.job_dir}/dftd4_results', 'w') as dftd4_file:
        dftd4_file.write(d4_out)

    for line in d4_out.split('\n'):
        if 'number of atoms' in line:
            n_atoms = int(line.split()[4])
        elif 'covCN                  q              C6A' in line:
            n_more = n_atoms
        elif n_more > 0:
            line = line.split()
            q.append(float(line[4]))
            c6.append(float(line[5]))
            c8.append(float(line[6]))
            alpha.append(float(line[7]))
            r_rel.append(float(line[8])**(1/3))
            n_more -= 1

    if inp.lennard_jones == 'd4':
        q, c6, c8, alpha, r_rel = average_equivalent_terms(topo, [q, c6, c8, alpha, r_rel])
        c6, c8, alpha, r_rel = [term[topo.unique_atomids] for term in [c6, c8, alpha, r_rel]]
        lj_a, lj_b = calc_c6_c12(inp, qm, topo, c6, c8, r_rel, inp.param)
    return q, lj_a, lj_b


def run_d4(inp):
    dftd4 = subprocess.Popen(['dftd4', '-c', str(inp.charge), inp.xyz_file],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    dftd4.wait()
    check_termination(dftd4)
    out = dftd4.communicate()[0].decode("utf-8")
    return out


def check_termination(process):
    if process.returncode != 0:
        print(process.communicate()[0].decode("utf-8"))
        raise RuntimeError({"DFTD4 run has terminated unsuccessfully"})


def calc_c6_c12(inp, qm, topo, c6s, c8s, r_rels, param):
    hartree2kjmol = 2625.499638
    bohr2ang = 0.52917721067
    bohr2nm = 0.052917721067
    new_ljs = []

    order2elem = [6, 1, 8, 7, 9]
    r_ref = {1: 1.986, 6: 2.083, 7: 1.641, 8: 1.452, 9: 1.58, 16: 1.5}
    s8_scale = {1: 0.133, 6: 0.133, 7: 0.683, 8: 0.683, 9: 0.683, 16: 0.5}

    for i, s8 in enumerate(param[::2]):
        s8_scale[order2elem[i]] = s8
    for i, r in enumerate(param[1::2]):
        r_ref[order2elem[i]] = r

    for i, (c6, c8, r_rel) in enumerate(zip(c6s, c8s, r_rels)):
        elem = qm.elements[topo.unique_atomids[i]]
        c8 *= s8_scale[elem]
        c10 = 40/49*(c8**2)/c6

        r_vdw = 2*r_ref[elem]*r_rel/bohr2ang
        r = np.arange(r_vdw*0.5, 20, 0.01)
        c12 = (c6 + c8/r_vdw**2 + c10/r_vdw**4) * r_vdw**6 / 2
        lj = c12/r**12 - c6/r**6 - c8/r**8 - c10/r**10
        weight = 10*(1-lj / min(lj))+1
        popt, _ = curve_fit(calc_lj, r, lj, absolute_sigma=False, sigma=weight)
        new_ljs.append(popt)

    new_ljs = np.array(new_ljs)*hartree2kjmol
    new_c6 = new_ljs[:, 0]*bohr2nm**6
    new_c12 = new_ljs[:, 1]*bohr2nm**12

    if inp.comb_rule != 1:
        new_a, new_b = calc_sigma_epsilon(new_c6, new_c12)
    else:
        new_a, new_b = new_c6, new_c12
    return new_a, new_b


def calc_lj(r, c6, c12):
    return c12/r**12 - c6/r**6


# def move_polarizability_from_hydrogens(alpha, mol):
#     new_alpha = np.zeros(mol.n_atoms)
#     for i, a_id in enumerate(mol.atomids):
#         if a_id == 1:
#             new_alpha[mol.neighbors[0][i][0]] += alpha[mol.atoms[i]]
#         else:
#             new_alpha[i] += alpha[mol.atoms[i]]
#     return new_alpha