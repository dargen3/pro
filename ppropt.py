import argparse
import json
import numba
import numpy as np
from Bio import SeqUtils
from Bio.PDB import Select, PDBIO, PDBParser, Superimposer, NeighborSearch
from dataclasses import dataclass
from os import system, path
from scipy.spatial.distance import cdist
from multiprocessing import Pool



def load_arguments():
    print("\nParsing arguments... ", end="")
    parser = argparse.ArgumentParser()
    parser.add_argument('--PDB_file', type=str, required=True,
                        help='PDB file with structure, which should be optimised.')
    parser.add_argument('--data_dir', type=str, required=True,
                        help='Directory for saving results.')
    parser.add_argument("--delete_auxiliary_files", help="Auxiliary calculation files can be large. With this argument, "
                                                         "the auxiliary files will be continuously deleted during the calculation.",
                        action="store_true")
    parser.add_argument('--cpu', type=int, required=False, default=1,
                        help='How many CPUs should be used for the calculation.')
    args = parser.parse_args()
    if not path.isfile(args.PDB_file):
        print(f"\nERROR! File {args.PDB_file} does not exist!\n")
        exit()
    print("ok")
    return args


class SelectIndexedResidues(Select):
    def accept_residue(self, residue):
        if residue.id[1] in self.indices:
            return 1
        else:
            return 0


@dataclass
class Residue:
    index: int
    constrained_atoms: list


@numba.jit(cache=True, nopython=True, fastmath=True, boundscheck=False, nogil=True)
def numba_dist(residue1, residue2):
     distances = np.empty(len(residue1))
     mins = np.empty(len(residue2))
     for i,a in enumerate(residue2):
         for j,b in enumerate(residue1):
             distances[j] = ((a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2)**(1/2)
         mins[i] = distances.min()
     return mins, mins.min()


def optimise_substructure(optimised_residue, PRO):

    # creation of substructure

    substructure_residues = []  # all residues in substructure
    optimised_residue_index = optimised_residue.id[1]
    constrained_atom_indices = []  # indices of substructure atoms, which should be constrained during optimisation
    counter_atoms = 1  # start from 1 because of xtb countering
    near_residues = sorted(PRO.nearest_residues[optimised_residue_index-1])
    for residue in near_residues:  # select substructure residues
        minimum_distances, absolute_min_distance = numba_dist(np.array([atom.coord for atom in optimised_residue.get_atoms()]),
                                                              np.array([atom.coord for atom in residue.get_atoms()]))
        if absolute_min_distance < 6:
            constrained_atoms = []
            for atom_distance, atom in zip(minimum_distances, residue.get_atoms()):
                if atom.name == "CA" or atom_distance > 4:
                    constrained_atoms.append(atom)
                    constrained_atom_indices.append(str(counter_atoms))
                counter_atoms += 1
            substructure_residues.append(Residue(index=residue.id[1],
                                                 constrained_atoms=constrained_atoms))
    substructure_data_dir = f"{PRO.data_dir}/sub_{optimised_residue_index}"
    system(f"mkdir {substructure_data_dir}")
    selector = SelectIndexedResidues()
    selector.indices = set([residue.index for residue in substructure_residues])
    PRO.io.save(f"{substructure_data_dir}/substructure.pdb", selector)

    # protonation of broken peptide bonds

    num_of_atoms = counter_atoms - 1
    system(f"cd {substructure_data_dir} ;"
           f"obabel -h -iPDB -oPDB substructure.pdb > reprotonated_substructure.pdb 2>/dev/null")
    with open(f"{substructure_data_dir}/reprotonated_substructure.pdb") as reprotonated_substructure_file:
        atom_lines = [line for line in reprotonated_substructure_file.readlines() if line[:4] == "ATOM"]
        original_atoms = atom_lines[:num_of_atoms]
        added_atoms = atom_lines[num_of_atoms:]
    with open(f"{substructure_data_dir}/repaired_substructure.pdb", "w") as repaired_substructure_file:
        repaired_substructure_file.write("".join(original_atoms))
        added_hydrogen_indices = []
        hydrogens_counter = num_of_atoms
        for line in added_atoms:
            res_i = int(line[22:26])
            if any((cdist(([float(x) for x in [line[30:38], line[38:46], line[46:54]]],),
                          (PRO.structure[res_i]["C"].coord,)) < 1.1,
                    cdist(([float(x) for x in [line[30:38], line[38:46], line[46:54]]],),
                          (PRO.structure[res_i]["N"].coord,)) < 1.1)):
                repaired_substructure_file.write(line)
                hydrogens_counter += 1
                added_hydrogen_indices.append(hydrogens_counter)
    system(f"cd {substructure_data_dir} ; mv repaired_substructure.pdb substructure.pdb")

    # optimise substructure by xtb
    xtb_settings_template = """$constrain
    atoms: xxx
    force constant=1.0
    $end
    $opt
    engine=rf
    $end
    """
    substructure_settings = xtb_settings_template.replace("xxx", ", ".join(
        constrained_atom_indices) + ", " + ", ".join([str(x) for x in added_hydrogen_indices]))
    with open(f"{substructure_data_dir}/xtb_settings.inp", "w") as xtb_settings_file:
        xtb_settings_file.write(substructure_settings)
    run_xtb = (f"cd {substructure_data_dir} ;"
               f"ulimit -s unlimited ;"
               f"export OMP_NUM_THREADS=1,1 ;"
               f"export OMP_MAX_ACTIVE_LEVELS=1 ;"
               f"export MKL_NUM_THREADS=1 ;"
               f"xtb substructure.pdb --gfnff --input xtb_settings.inp --opt tight --alpb water --verbose > xtb_output.txt 2>&1 ; rm gfnff_*")
    system(run_xtb)
    if not path.isfile(f"{substructure_data_dir}/xtbopt.pdb"):  # second try by L-ANCOPT
        substructure_settings = open(f"{substructure_data_dir}/xtb_settings.inp", "r").read().replace("rf", "lbfgs")
        with open(f"{substructure_data_dir}/xtb_settings.inp", "w") as xtb_settings_file:
            xtb_settings_file.write(substructure_settings)
        system(run_xtb)

    if path.isfile(f"{substructure_data_dir}/xtbopt.pdb"):
        category = "Optimised residue"
        optimised_substructure = PDBParser(QUIET=True).get_structure("substructure", f"{substructure_data_dir}/xtbopt.pdb")[0]
        optimised_substructure_residues = list(list(optimised_substructure.get_chains())[0].get_residues())


        sup = Superimposer()
        # todo přepsat není potřeba
        sup.set_atoms([atom for residue in substructure_residues for atom in residue.constrained_atoms if atom.name == "CA"], [ atom for atom in list(list(optimised_substructure.get_chains())[0].get_atoms()) if atom.name == "CA"])

        sup.apply(optimised_substructure.get_atoms())
        for op_res, or_res in zip(optimised_substructure_residues, substructure_residues):
            or_res.coordinates = [a.coord for a in op_res.get_atoms()]
    else:
        category = "Not optimised residue"
        substructure_residues = None

    log = {"residue index": optimised_residue_index,
           "residue name": SeqUtils.IUPACData.protein_letters_3to1[optimised_residue.resname.capitalize()],
           "category": category}
    return log, substructure_residues








class PRO:
    def __init__(self,
                 data_dir: str,
                 PDB_file: str,
                 cpu: int,
                 delete_auxiliary_files: bool):
        self.data_dir = data_dir
        self.PDB_file = PDB_file
        self.cpu = cpu
        self.delete_auxiliary_files = delete_auxiliary_files

    def optimise(self):
        self._prepare_directory()
        self._load_molecule()
        with Pool(self.cpu) as p:

            logs = []
            for round_residues in self.sorted_residues:
                optimisations = p.starmap(optimise_substructure, [(residue, self) for residue in round_residues])
                for log, optimised_residues in optimisations:
                    logs.append(log)
                    if optimised_residues:
                        for optimised_residue in optimised_residues:
                            for coord, or_atom in zip(optimised_residue.coordinates, self.residues[optimised_residue.index-1].get_atoms()):
                                or_atom.coord = coord



        # doplnit rmsd, případně upravit category


        with open(f"{self.data_dir}/residues.logs", "w") as residues_logs:
            residues_logs.write(json.dumps(sorted(logs, key=lambda x: x['residue index']), indent=2))
        self.io.save(f"{self.data_dir}/optimised_PDB/{path.basename(self.PDB_file[:-4])}_optimised.pdb")
        if self.delete_auxiliary_files:
            system(f"for au_file in {self.data_dir}/sub_* ; do rm -fr $au_file ; done &")
        print(f"Structure succesfully optimised.\n")

    def _prepare_directory(self):
        print("\nPreparing a data directory... ", end="")
        if path.exists(self.data_dir):
            exit(f"\n\nError! Directory with name {self.data_dir} exists. "
                 f"Remove existed directory or change --data_dir argument.")
        system(f"mkdir {self.data_dir};"
               f"mkdir {self.data_dir}/inputed_PDB;"
               f"mkdir {self.data_dir}/optimised_PDB;"
               f"cp {self.PDB_file} {self.data_dir}/inputed_PDB")
        print("ok\n")

    def _load_molecule(self):
        print(f"Loading of structure from {self.PDB_file}... ", end="")
        try:
            structure = PDBParser(QUIET=True).get_structure("structure", self.PDB_file)
            io = PDBIO()
            io.set_structure(structure)
            self.io = io
            self.structure = io.structure[0]["A"]
        except KeyError:
            print(f"\nERROR! PDB file {self.PDB_file} does not contain any structure.\n")
            exit()
        self.residues = list(self.structure.get_residues())
        kdtree = NeighborSearch(list(self.structure.get_atoms()))

        amk_radius = {'ALA': 2.4801,
                      'ARG': 4.8618,
                      'ASN': 3.2237,
                      'ASP': 2.8036,
                      'CYS': 2.5439,
                      'GLN': 3.8456,
                      'GLU': 3.3963,
                      'GLY': 2.1455,
                      'HIS': 3.8376,
                      'ILE': 3.4050,
                      'LEU': 3.5357,
                      'LYS': 4.4521,
                      'MET': 4.1821,
                      'PHE': 4.1170,
                      'PRO': 2.8418,
                      'SER': 2.4997,
                      'THR': 2.7487,
                      'TRP': 4.6836,
                      'TYR': 4.5148,
                      'VAL': 2.9515}

        self.nearest_residues = [set(kdtree.search(residue.center_of_mass(geometric=True), amk_radius[residue.resname]+10, level="R"))
                                 for residue in self.residues]


        self.density_of_atoms_around_residues = []
        for residue in self.residues:
            volume_c = ((4 / 3) * 3.14 * ((amk_radius[residue.resname]) +3) ** 3)
            num_of_atoms_c = len(set(kdtree.search(residue.center_of_mass(geometric=True), (amk_radius[residue.resname]) +2, level="A")))
            density_c = num_of_atoms_c/volume_c

            volume_2c = ((4 / 3) * 3.14 * ((amk_radius[residue.resname]) +10) ** 3)
            num_of_atoms_2c = len(set(kdtree.search(residue.center_of_mass(geometric=True), (amk_radius[residue.resname]) +5, level="A")))
            density_2c = num_of_atoms_2c/volume_2c

            volume_3c = ((4 / 3) * 3.14 * ((amk_radius[residue.resname]) +15) ** 3)
            num_of_atoms_3c = len(set(kdtree.search(residue.center_of_mass(geometric=True), (amk_radius[residue.resname]) +10, level="A")))
            density_3c = num_of_atoms_3c/volume_3c

            self.density_of_atoms_around_residues.append(density_c + density_2c/10 + density_3c/20)


        unsorted_residues = [res for res in self.residues]
        sorted_residues = []
        already_sorted = [False for _ in range(len(self.residues))]
        while len(unsorted_residues):
            round_residues = []
            for res in unsorted_residues:
                for near_residue in self.nearest_residues[res.id[1]-1]:
                    if res == near_residue:
                        continue
                    if already_sorted[near_residue.id[1]-1]:
                        continue
                    if self.density_of_atoms_around_residues[near_residue.id[1]-1] > self.density_of_atoms_around_residues[res.id[1]-1]:
                        break
                else:
                    round_residues.append(res)
            for res in round_residues:
                unsorted_residues.remove(res)
                already_sorted[res.id[1]-1] = True
            sorted_residues.append(round_residues)
        self.sorted_residues = sorted_residues
        print("ok\n")


if __name__ == '__main__':
    args = load_arguments()
    PRO(args.data_dir, args.PDB_file, args.cpu, args.delete_auxiliary_files).optimise()


# B
# 0.2314937213030495
# 0.1942285162060808
# 0.1460290694882233

# O
# 0.33201932431805353
# 0.28619598154219567
# 0.28383192434012156

# A
# 0.20287618627567075
# 0.16309199469263264
# 0.15014294039213372

# Q
# 0.2940527720748443
# 0.2759138088217413
# 0.2761768060077968