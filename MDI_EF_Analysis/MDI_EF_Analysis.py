import sys
import time
import argparse
import numpy as np
import pandas as pd

# Use local MDI build
import mdi.MDI_Library as mdi

try:
    from mpi4py import MPI
    use_mpi4py = True
except ImportError:
    use_mpi4py = False


# Get the MPI communicator
if use_mpi4py:
    mpi_world = MPI.COMM_WORLD
else:
    mpi_world = None

def mdi_checks(mdi_engine):
    """
    Perform checks on the MDI driver we have accepted to make sure it fits this analysis.
    """
    # Confirm that this code is being used as a driver
    role = mdi_engine.MDI_Get_Role()
    if not role == mdi_engine.MDI_DRIVER:
        raise Exception("Must run driver_py.py as a DRIVER")

    # Connect to the engine
    engine_comm = mdi_engine.MDI_NULL_COMM
    nengines = 1
    for iengine in range(nengines):
        comm = mdi.MDI_Accept_Communicator()

        # Determine the name of the engine
        mdi_engine.MDI_Send_Command("<NAME", comm)
        name = mdi_engine.MDI_Recv(mdi.MDI_NAME_LENGTH, mdi.MDI_CHAR, comm)

        print(F"Engine name: {name}")

        if name == "NO_EWALD":
            if engine_comm != mdi_engine.MDI_NULL_COMM:
                raise Exception("Accepted a communicator from a second NO_EWALD engine.")
            engine_comm = comm
        else:
            raise Exception("Unrecognized engine name.")

    return engine_comm


if __name__ == "__main__":

    ###########################################################################
    #
    #   Handle user arguments
    #
    ###########################################################################

    # Handle arguments with argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-mdi", help="flags for mdi", type=str, required=True)
    parser.add_argument("-snap", help="the snapshot file to process", type=str,
        required=True)
    parser.add_argument("-probes", help="indices of the probe atoms", type=str,
        required=True)
    parser.add_argument("--byres", help="give electric field at probe by residue",
        action="store_true")
    parser.add_argument("--bymol", help="give electric field at probe by molecule",
        action="store_true")

    args = parser.parse_args()

    # Process args for MDI
    mdi.MDI_Init(args.mdi, mpi_world)
    if use_mpi4py:
        mpi_world = mdi.MDI_Get_Intra_Code_MPI_Comm()
        world_rank = mpi_world.Get_rank()

    snapshot_filename = args.snap
    probes = [int(x) for x in args.probes.split()]

    if args.byres and args.bymol:
        parser.error("--byres and --bymol cannot be used together. Please only use one.")

    engine_comm = mdi_checks(mdi)

    # Print the probe atoms
    print(F"Probes: {probes}")

    ###########################################################################
    #
    #   Get Information from Tinker
    #
    ###########################################################################

    # Get the number of atoms
    mdi.MDI_Send_Command("<NATOMS", engine_comm)
    natoms_engine = mdi.MDI_Recv(1, mdi.MDI_INT, engine_comm)
    print(F"natoms: {natoms_engine}")

    # Get the number of multipole centers
    mdi.MDI_Send_Command("<NPOLES", engine_comm)
    npoles = mdi.MDI_Recv(1, mdi.MDI_INT, engine_comm)
    print("npoles: " + str(npoles))

    # Get the indices of the mulitpole centers per atom
    mdi.MDI_Send_Command("<IPOLES", engine_comm)
    ipoles = mdi.MDI_Recv(natoms_engine, mdi.MDI_INT, engine_comm)

    # Get the molecule information
    mdi.MDI_Send_Command("<MOLECULES", engine_comm)
    molecules = np.array(mdi.MDI_Recv(natoms_engine, mdi.MDI_INT, engine_comm))

    # Get the residue information
    mdi.MDI_Send_Command("<RESIDUES", engine_comm)
    residues = np.array(mdi.MDI_Recv(natoms_engine, mdi.MDI_INT, engine_comm))


    ###########################################################################
    #
    #   Calculate Indices
    #
    ###########################################################################

    ## Bookkeeping

    # Probe is given as atom number by user, but this may not correspond
    # to the pole index. We have to get it from ipoles which gives
    # the pole index for each atom. We subtract 1 because python
    # indexes from 0, but the original (fortran) indexes from one.
    # ipoles is length natom and gives the pole number for each atom.
    # probe_pole_indices gives the pole number (starts at 1 because we are passing)
    # to Tinker
    probe_pole_indices = [int(ipoles[atom_number-1]) for atom_number in probes]
    print(F'Probe pole indices {probe_pole_indices}')

    # Get the atom and pole numbers for the molecules/residues of interest.
    interest_atoms = []
    atoms_pole_numbers = []
    if args.bymol:
        by_type = 'molecule'
        from_fragment = np.unique(molecules)
        for mol in from_fragment:
            # These are the atom numbers for the atoms in the specified molecules
            molecule_atoms = np.array(np.where(molecules == mol)) + 1
            # The pole indices for the speified molecule
            pole_numbers = [ipoles[atom_index - 1] for atom_index in molecule_atoms[0]]
            interest_atoms.append(molecule_atoms[0])
            atoms_pole_numbers.append(np.array(pole_numbers))
    elif args.byres:
        by_type = 'residue'
        from_fragment = np.unique(residues)
        for res in from_fragment:
            # These are the atom numbers for the atoms in the specified residues
            residue_atoms = np.array(np.where(residues == res)) + 1
            # The pole indices for the speified molecule
            pole_numbers = [ipoles[atom_index - 1] for atom_index in residue_atoms[0]]
            interest_atoms.append(residue_atoms[0])
            atoms_pole_numbers.append(np.array(pole_numbers))
    else:
        by_type = 'atom'
        # We are interested in all of the atoms.
        interest_atoms = list(range(1,natoms_engine+1))
        from_fragment = interest_atoms.copy()
        atoms_pole_numbers = np.array([[x] for x in ipoles])

    ###########################################################################
    #
    #   Send Probe Information to Tinker
    #
    ###########################################################################

    # Inform Tinker of the probe atoms
    mdi.MDI_Send_Command(">NPROBES", engine_comm)
    mdi.MDI_Send(len(probes), 1, mdi.MDI_INT, engine_comm)
    mdi.MDI_Send_Command(">PROBES", engine_comm)
    mdi.MDI_Send(probe_pole_indices, len(probes), mdi.MDI_INT, engine_comm)

    angstrom_to_bohr = mdi.MDI_Conversion_Factor("angstrom","atomic_unit_of_length")

    ###########################################################################
    #
    #   Engine and Trajectory File Compatibility Check.
    #
    ###########################################################################

    # Check that engine and trajectory are compatible.
    # Process first two lines of snapshot to get information.
    with open(snapshot_filename,"r") as snapshot_file:
        first_line = snapshot_file.readline()
        natoms = int(first_line.split()[0])
        second_line = snapshot_file.readline().split()
        if len(second_line) == 6:
            # This line gives box information if length is 6.
            # This means we will need to skip two lines for every frame.
            skip_line = 2
        else:
            skip_line = 1

    if natoms != natoms_engine:
        raise Exception(F"Snapshot file and engine have inconsistent number of atoms \
                            Engine : {natoms_engine} \n Snapshot File : {natoms}")

    ###########################################################################
    #
    #   Read Trajectory and do analysis.
    #
    ###########################################################################

    # Read trajectory and do analysis
    for snapshot in pd.read_csv(snapshot_filename, chunksize=natoms+skip_line,
        header=None, delim_whitespace=True, names=range(8),
        skiprows=skip_line, index_col=None):

        # Pull out just coords, convert to numeric and use conversion factor.
        # columns 2-4 of the pandas dataframe are the coordinates.
        # Must create a copy to send to MDI.
        snapshot_coords = (snapshot.iloc[:natoms , 2:5].apply(pd.to_numeric) *
            angstrom_to_bohr).to_numpy().copy()

        mdi.MDI_Send_Command(">COORDS", engine_comm)
        mdi.MDI_Send(snapshot_coords, 3*natoms, mdi.MDI_DOUBLE, engine_comm)

        # Get the electric field information
        # mdi.MDI_Send_Command("<FIELD", engine_comm)
        # field = np.zeros(3 * npoles, dtype='float64')
        # mdi.MDI_Recv(3*npoles, mdi.MDI_DOUBLE, engine_comm, buf = field)
        # field = field.reshape(npoles,3)

        # Get the pairwise DFIELD
        dfield = np.zeros((len(probes),npoles,3))
        mdi.MDI_Send_Command("<DFIELD", engine_comm)
        mdi.MDI_Recv(3*npoles*len(probes), mdi.MDI_DOUBLE, engine_comm, buf=dfield)

        # Get the pairwise UFIELD
        ufield = np.zeros((len(probes),npoles,3))
        mdi.MDI_Send_Command("<UFIELD", engine_comm)
        mdi.MDI_Recv(3*npoles*len(probes), mdi.MDI_DOUBLE, engine_comm, buf=ufield)

        # Print dfield for the first probe atom
        #print("DFIELD; UFIELD: ")
        #for ipole in range(min(npoles, 10)):
            #print("   " + str(dfield[0][ipole]) + "; " + str(ufield[0][ipole]) )


        # Sum the appropriate values

        columns = ['Probe Atom', 'Probe Coordinates']
        columns += [F'{by_type} {x}' for x in from_fragment]
        dfield_df = pd.DataFrame(columns=columns)
        ufield_df = pd.DataFrame(columns=columns)

        # Get sum at each probe (total)
        for i in range(len(probes)):
            to_add_dfield = {'Probe Atom': probes[i]}
            to_add_dfield['Probe Coordinates'] = snapshot_coords[probes[i]]
            to_add_ufield = {'Probe Atom': probes[i]}
            to_add_ufield['Probe Coordinates'] = snapshot_coords[probes[i]]

            for fragment_index, fragment in enumerate(atoms_pole_numbers):
                dfield_at_probe_due_to_fragment = dfield[i, fragment-1].sum(axis=0)
                to_add_dfield[F'{by_type} {from_fragment[fragment_index]}'] = dfield_at_probe_due_to_fragment

                ufield_at_probe_due_to_fragment = ufield[i, fragment-1].sum(axis=0)
                to_add_ufield[F'{by_type} {from_fragment[fragment_index]}'] = ufield_at_probe_due_to_fragment

            dfield_df = dfield_df.append(to_add_dfield, ignore_index=True)
            ufield_df = ufield_df.append(to_add_ufield, ignore_index=True)

    dfield_df.to_csv('dfield.csv', index=False)
    ufield_df.to_csv('ufield.csv', index=False)


    # Send the "EXIT" command to the engine
    mdi.MDI_Send_Command("EXIT", engine_comm)

    # Ensure that all ranks have terminated
    if use_mpi4py:
        MPI.COMM_WORLD.Barrier()
