import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from functools import _make_key, lru_cache
from typing import DefaultDict, List, Tuple

if sys.version_info >= (3, 9):
    from collections.abc import Iterator, Mapping
else:
    from typing import Iterator, Mapping

import numpy as np

from yt._typing import ParticleCoordinateTuple
from yt.geometry.selection_routines import GridSelector
from yt.utilities.on_demand_imports import _h5py as h5py

io_registry = {}

use_caching = 0


def _make_io_key(args, *_args, **kwargs):
    self, obj, field, ctx = args
    # Ignore self because we have a self-specific cache
    return _make_key((obj.id, field), *_args, **kwargs)


class BaseIOHandler:
    _vector_fields: Tuple[str, ...] = ()
    _dataset_type: str
    _particle_reader = False
    _cache_on = False
    _misses = 0
    _hits = 0

    def __init_subclass__(cls, *args, **kwargs):
        super().__init_subclass__(*args, **kwargs)
        if hasattr(cls, "_dataset_type"):
            io_registry[cls._dataset_type] = cls
        if use_caching and hasattr(cls, "_read_obj_field"):
            cls._read_obj_field = lru_cache(
                maxsize=use_caching, typed=True, make_key=_make_io_key
            )(cls._read_obj_field)

    def __init__(self, ds):
        self.queue = defaultdict(dict)
        self.ds = ds
        self._last_selector_id = None
        self._last_selector_counts = None
        self._array_fields = {}
        self._cached_fields = {}
        # Make sure _vector_fields is a dict of fields and their dimension
        # and assume all non-specified vector fields are 3D
        if not isinstance(self._vector_fields, dict):
            # note, this type change can cause some mypy errors.
            self._vector_fields = {field: 3 for field in self._vector_fields}

    # We need a function for reading a list of sets
    # and a function for *popping* from a queue all the appropriate sets
    @contextmanager
    def preload(self, chunk, fields: List[Tuple[str, str]], max_size):
        yield self

    def peek(self, grid, field):
        return self.queue[grid.id].get(field, None)

    def push(self, grid, field, data):
        if grid.id in self.queue and field in self.queue[grid.id]:
            raise ValueError
        self.queue[grid][field] = data

    def _field_in_backup(self, grid, backup_file, field_name):
        if os.path.exists(backup_file):
            fhandle = h5py.File(backup_file, mode="r")
            g = fhandle["data"]
            grid_group = g["grid_%010i" % (grid.id - grid._id_offset)]
            if field_name in grid_group:
                return_val = True
            else:
                return_val = False
            fhandle.close()
            return return_val
        else:
            return False

    def _read_data_set(self, grid, field):
        # check backup file first. if field not found,
        # call frontend-specific io method
        backup_filename = grid.ds.backup_filename
        if not os.path.exists(backup_filename):
            return self._read_data(grid, field)
        elif self._field_in_backup(grid, backup_filename, field):
            fhandle = h5py.File(backup_filename, mode="r")
            g = fhandle["data"]
            grid_group = g["grid_%010i" % (grid.id - grid._id_offset)]
            data = grid_group[field][:]
            fhandle.close()
            return data
        else:
            return self._read_data(grid, field)

    # Now we define our interface
    def _read_data(self, grid, field):
        pass

    def _read_fluid_selection(
        self, chunks, selector, fields: List[Tuple[str, str]], size
    ) -> Mapping[Tuple[str, str], np.ndarray]:
        # This function has an interesting history.  It previously was mandate
        # to be defined by all of the subclasses.  But, to avoid having to
        # rewrite a whole bunch of IO handlers all at once, and to allow a
        # better abstraction for grid-based frontends, we're now defining it in
        # the base class.
        rv = {}
        nodal_fields = []
        for field in fields:
            finfo = self.ds.field_info[field]
            nodal_flag = finfo.nodal_flag
            if np.any(nodal_flag):
                num_nodes = 2 ** sum(nodal_flag)
                rv[field] = np.empty((size, num_nodes), dtype="=f8")
                nodal_fields.append(field)
            else:
                rv[field] = np.empty(size, dtype="=f8")
        ind = {field: 0 for field in fields}
        for field, obj, data in self.io_iter(chunks, fields):
            if data is None:
                continue
            if isinstance(selector, GridSelector) and field not in nodal_fields:
                ind[field] += data.size
                rv[field] = data.copy()
            else:
                ind[field] += obj.select(selector, data, rv[field], ind[field])
        return rv

    def io_iter(self, chunks, fields: List[Tuple[str, str]]):
        raise NotImplementedError(
            "subclassing Dataset.io_iter this is required in order to use the default "
            "implementation of Dataset._read_fluid_selection. "
            "Custom implementations of the latter may not rely on this method."
        )

    def _read_data_slice(self, grid, field, axis, coord):
        sl = [slice(None), slice(None), slice(None)]
        sl[axis] = slice(coord, coord + 1)
        tr = self._read_data_set(grid, field)[tuple(sl)]
        if tr.dtype == "float32":
            tr = tr.astype("float64")
        return tr

    def _read_field_names(self, grid):
        pass

    @property
    def _read_exception(self):
        return None

    def _read_chunk_data(self, chunk, fields):
        return {}

    def _count_particles_chunks(
        self,
        psize: DefaultDict[str, int],
        chunks,
        ptf: DefaultDict[str, List[str]],
        selector,
    ) -> DefaultDict[str, int]:
        """
        Counts particles by particle type across chunks for a selector object

        Parameters
        ----------
        psize : defaultdict
            mapping of particle type to the number of particles
        chunks
            the chunks of a dataset Index
        ptf : defaultdict[list]
            particle type fields (ptf), the fields currently being read organized
            by particle type. e.g., ptf['PartType0'] = ['density', 'mass', ...]
        selector
            the selector object of a YTSelectionContainer

        Returns
        -------
        defaultdict
            updated psize dictionary
        """
        # in this base class, we always manually count particles in the selection.
        # This does get overridden in the subclass for Particle, since in that
        # case we know that the chunks are composed to DataFiles.
        return self._count_selected_particles(psize, chunks, ptf, selector)

    def _count_selected_particles(
        self,
        psize: DefaultDict[str, int],
        chunks,
        ptf: DefaultDict[str, List[str]],
        selector,
    ) -> DefaultDict[str, int]:
        # counts the number of particles in a selection by chunk, same arguments
        # as self._count_particles_chunks.
        for ptype, (x, y, z), hsml in self._read_particle_coords(chunks, ptf):
            # assume particles have zero radius, we break this assumption
            # in the SPH frontend and override this function there
            psize[ptype] += selector.count_points(x, y, z, hsml)
        return psize

    def _read_particle_coords(
        self, chunks, ptf: DefaultDict[str, List[str]]
    ) -> Iterator[ParticleCoordinateTuple]:
        # An iterator that yields particle coordinates for each chunk by particle
        # type. Must be implemented by each frontend. Must yield a tuple of
        # (particle type, xyz, hsml) by chunk. If the frontend does not have
        # a smoothing length, yield (particle type, xyz, 0.0)
        raise NotImplementedError

    def _read_particle_data_file(self, data_file, ptf, selector=None):
        # each frontend needs to implement this: read from a data_file object
        # and return a dict of fields for that data_file
        raise NotImplementedError

    def _read_particle_selection(
        self, chunks, selector, fields: List[Tuple[str, str]]
    ) -> Mapping[Tuple[str, str], np.ndarray]:
        rv = {}  # the return dictionary
        ind = {}  # holds the most recent max index of the return arrays by field

        # Initialize containers for tracking particle, field information
        # ptf (particle field types) maps particle type to list of on-disk fields to read
        # psize maps particle type to on-disk size across chunks
        # fsize maps particle type to size of return values
        # field_maps stores fields, accounting for field unions
        ptf: DefaultDict[str, List[str]] = defaultdict(list)
        psize: DefaultDict[str, int] = defaultdict(lambda: 0)
        fsize: DefaultDict[Tuple[str, str], int] = defaultdict(lambda: 0)
        field_maps: DefaultDict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(
            list
        )

        # We first need a set of masks for each particle type
        chunks = list(chunks)
        unions = self.ds.particle_unions
        # What we need is a mapping from particle types to return types
        for field in fields:
            ftype, fname = field
            fsize[field] = 0
            # We should add a check for p.fparticle_unions or something here
            if ftype in unions:
                for pt in unions[ftype]:
                    ptf[pt].append(fname)
                    field_maps[pt, fname].append(field)
            else:
                ptf[ftype].append(fname)
                field_maps[field].append(field)
        # Now we have our full listing

        # Now we add particle counts across chunks to psize
        self._count_particles_chunks(psize, chunks, ptf, selector)

        # Now we allocate
        for field in fields:
            if field[0] in unions:
                for pt in unions[field[0]]:
                    fsize[field] += psize.get(pt, 0)
            else:
                fsize[field] += psize.get(field[0], 0)
        shape: Tuple[int, ...]
        for field in fields:
            if field[1] in self._vector_fields:
                vsize = self._vector_fields[field[1]]  # type:ignore
                # note: the above line causes a mypy failure due to how we
                # convert _vector_fields from a tuple to dict in __init__. mypy
                # is expecting a tuple here. And since _vector_fields is used in
                # many places, just ignoring for now...
                shape = (fsize[field], vsize)
            elif field[1] in self._array_fields:
                shape = (fsize[field],) + self._array_fields[field[1]]
            else:
                shape = (fsize[field],)
            rv[field] = np.empty(shape, dtype="float64")
            ind[field] = 0
        # Now we read.
        for field_r, vals in self._read_particle_fields(chunks, ptf, selector):
            # Note that we now need to check the mappings
            for field_f in field_maps[field_r]:
                my_ind = ind[field_f]
                # mylog.debug("Filling %s from %s to %s with %s",
                #    field_f, my_ind, my_ind+vals.shape[0], field_r)
                rv[field_f][my_ind : my_ind + vals.shape[0], ...] = vals
                ind[field_f] += vals.shape[0]
        # Now we need to truncate all our fields, since we allow for
        # over-estimating.
        for field_f in ind:
            rv[field_f] = rv[field_f][: ind[field_f]]
        return rv

    def _read_particle_fields(self, chunks, ptf, selector):
        # Now we have all the sizes, and we can allocate
        data_files = set()
        for chunk in chunks:
            for obj in chunk.objs:
                data_files.update(obj.data_files)
        for data_file in sorted(data_files, key=lambda x: (x.filename, x.start)):
            data_file_data = self._read_particle_data_file(data_file, ptf, selector)
            # temporary trickery so it's still an iterator, need to adjust
            # the io_handler.BaseIOHandler.read_particle_selection() method
            # to not use an iterator.
            yield from data_file_data.items()


# As a note: we don't *actually* want this to be how it is forever.  There's no
# reason we need to have the fluid and particle IO handlers separated.  But,
# for keeping track of which frontend is which, this is a useful abstraction.
class BaseParticleIOHandler(BaseIOHandler):
    def _sorted_chunk_iterator(self, chunks):
        chunks = list(chunks)
        data_files = set()
        for chunk in chunks:
            for obj in chunk.objs:
                data_files.update(obj.data_files)
        yield from sorted(data_files, key=lambda x: (x.filename, x.start))

    def _count_particles_chunks(
        self,
        psize: DefaultDict[str, int],
        chunks,
        ptf: DefaultDict[str, List[str]],
        selector,
    ) -> DefaultDict[str, int]:
        if getattr(selector, "is_all_data", False):
            for data_file in self._sorted_chunk_iterator(chunks):
                for ptype in ptf.keys():
                    psize[ptype] += data_file.total_particles[ptype]
        else:
            # we must apply the selector and count the result
            psize = self._count_selected_particles(psize, chunks, ptf, selector)
        return psize


class IOHandlerExtracted(BaseIOHandler):

    _dataset_type = "extracted"

    def _read_data_set(self, grid, field):
        return grid.base_grid[field] / grid.base_grid.convert(field)

    def _read_data_slice(self, grid, field, axis, coord):
        sl = [slice(None), slice(None), slice(None)]
        sl[axis] = slice(coord, coord + 1)
        return grid.base_grid[field][tuple(sl)] / grid.base_grid.convert(field)
