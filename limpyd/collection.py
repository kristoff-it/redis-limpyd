# -*- coding:utf-8 -*-
from __future__ import unicode_literals
from future.builtins import object
from collections import namedtuple


from limpyd.utils import unique_key
from limpyd.exceptions import *
from limpyd.fields import MultiValuesField


ParsedFilter = namedtuple('ParsedFilter', ['index', 'suffix', 'extra_field_parts', 'value'])


class CollectionManager(object):
    """
    Retrieve objects collection, optionnaly slice and order it.
    CollectionManager is lazy: it will call redis only when evaluating it
    (iterating, slicing, forcing as list...)

    API:
    MyModel.collection(**filters) => return the whole collection, eventually filtered.
    MyModel.collection().sort(by='field') => return the collection sorted.
    MyModel.collection().sort(by='field')[:10] => slice the sorted collection.
    MyModel.collection().instances() => return the instances

    Note:
    Slicing a collection will force a sort.
    """

    _accepted_key_types = {'set'}  # Type of keys indexes are allowed to return

    def __init__(self, cls):
        self.cls = cls
        self._lazy_collection = {  # Store infos to make the requested collection
            'sets': [],  # store sets to use (we'll intersect them)
            'pks': set(),  # store special filter on pk
        }
        self._instances = False  # True when instances are asked
                                 # instead of raw pks
        self._instances_skip_exist_test = False  # If True will return instances
                                                 # without testing if pk exist
        self._sort = None  # Will store sorting parameters
        self._slice = None  # Will store slice parameters (start and num)
        self._len = None  # Store the result of the final collection, to avoid
                          # having to compute the whole thing twice when doing
                          # `list(Model.collection)` (a `list` will call
                          # __iter__ AND __len__)
        self._len_mode = True   # Set to True if the __len__ method is directly
                                # called (to avoid some useless computations)
                                # True by default to manage the __iter__ + __len__
                                # case, specifically set to False in other cases

    def __iter__(self):
        old_slice_and_len_mode = None if self._slice is None else self._slice.copy(), self._len_mode
        try:
            self._len_mode = False
            return self._collection.__iter__()
        finally:
            self._slice, self._len_mode = old_slice_and_len_mode

    def __getitem__(self, arg):
        old_slice_and_len_mode = None if self._slice is None else self._slice.copy(), self._len_mode
        try:
            self._len_mode = False
            self._slice = {}
            if isinstance(arg, slice):
                # A slice has been requested
                # so add it to the sort parameters (via slice)
                # and return the collection (a scliced collection is no more
                # chainable, so we do not return `self`)
                start = arg.start or 0
                if start < 0:
                    # in case of a negative start, we can't use redis sort so
                    # we fetch all the collection before returning the wanted slice
                    return self._collection[arg]
                self._slice['start'] = start
                stop = arg.stop
                # Redis expects a number of elements
                # not a python style stop value
                if stop is None:
                    # negative value for the count return all
                    self._slice['num'] = -1
                else:
                    self._slice['num'] = stop - start
                return self._collection
            else:
                # A single item has been requested
                # Nevertheless, use the redis pagination, to minimize
                # data transfert and use the fast redis offset system
                start = arg
                if start >= 0:
                    self._slice['start'] = start
                    self._slice['num'] = 1  # one element
                    return self._collection[0]
                else:
                    # negative index, we have to fetch the whole collection first
                    return self._collection[start]
        finally:
            self._slice, self._len_mode = old_slice_and_len_mode

    def _get_pk(self):
        """
        Return None if we don't have any filter on a pk, the pk if we have one,
        or raise a ValueError if we have more than one.
        For internal use only.
        """
        pk = None
        if self._lazy_collection['pks']:
            if len(self._lazy_collection['pks']) > 1:
                raise ValueError('Too much pks !')
            pk = list(self._lazy_collection['pks'])[0]
        return pk

    def _prepare_sort_options(self, has_pk):
        """
        Prepare "sort" options to use when calling the collection, depending
        on "_sort" and "_slice" attributes
        """
        sort_options = {}
        if self._sort is not None and not has_pk:
            sort_options.update(self._sort)
        if self._slice is not None:
            sort_options.update(self._slice)
        if not sort_options and self._sort is None:
            sort_options = None
        return sort_options

    @property
    def _collection(self):
        """
        Effectively retrieve data according to lazy_collection.
        """
        old_slice_and_len_mode = None if self._slice is None else self._slice.copy(), self._len_mode
        try:  # try block to always reset the _slice in the "finally" part

            conn = self.cls.get_connection()
            self._len = 0

            # The collection fails (empty) if more than one pk or if the only one
            # doesn't exists
            try:
                pk = self._get_pk()
            except ValueError:
                return []
            else:
                if pk is not None and not self.cls.get_field('pk').exists(pk):
                    return []

            # Prepare options and final set to get/sort
            sort_options = self._prepare_sort_options(bool(pk))

            final_set, keys_to_delete = self._get_final_set(
                                                self._lazy_collection['sets'],
                                                pk, sort_options)

            if self._len_mode:
                if final_set is None:
                    # final_set is None if we have a only pk
                    self._len = 1
                else:
                    self._len = self._collection_length(final_set)
                    if keys_to_delete:
                        conn.delete(*keys_to_delete)

                # return nothing
                return

            else:

                # fill the collection
                if final_set is None:
                    # final_set is None if we have a pk without other sets, and no
                    # needs to get values so we can simply return the pk
                    collection = set([pk])
                else:
                    # compute the sets and call redis te retrieve wanted values
                    collection = self._final_redis_call(final_set, sort_options)
                    if keys_to_delete:
                        conn.delete(*keys_to_delete)

                # Format return values if needed
                return self._prepare_results(collection)
        finally:
            self._slice, self._len_mode = old_slice_and_len_mode

    def _final_redis_call(self, final_set, sort_options):
        """
        The final redis call to obtain the values to return from the "final_set"
        with some sort options.
        """
        conn = self.cls.get_connection()
        if sort_options is not None:
            # a sort, or values, call the SORT command on the set
            return conn.sort(final_set, **sort_options)
        else:
            # no sort, nor values, simply return the full set
            return conn.smembers(final_set)

    def _collection_length(self, final_set):
        """
        Return the length of the final collection, directly asking redis for the
        count without calling sort
        """
        return self.cls.get_connection().scard(final_set)

    def _to_instances(self, pks):
        """
        Returns a list of instances for each given pk, respecting the condition
        about checking or not if a pk exists.
        """
        # we want instances, so create an object for each pk, without
        # checking for pk existence if asked
        meth = self.cls.lazy_connect if self._instances_skip_exist_test else self.cls
        return [meth(pk) for pk in pks]

    def _prepare_results(self, results):
        """
        Called in _collection to prepare results from redis before returning
        them.
        """
        if self._instances:
            results = self._to_instances(results)
        else:
            results = list(results)

        # cache the len for future use
        self._len = len(results)

        return results

    def _prepare_sets(self, sets):
        """
        Return all sets in self._lazy_collection['sets'] to be ready to be used
        to intersect them. Called by _get_final_set, to use in subclasses.
        Must return a tuple with a set of redis set keys, and another with
        new temporary keys to drop at the end of _get_final_set
        """

        final_sets = set()
        tmp_keys = set()

        for set_ in sets:
            if isinstance(set_, str):
                final_sets.add(set_)
            elif isinstance(set_, ParsedFilter):

                for index_key, key_type, is_tmp in set_.index.get_filtered_keys(
                            set_.suffix,
                            accepted_key_types=self._accepted_key_types,
                            *(set_.extra_field_parts + [set_.value])
                        ):
                    if key_type not in self._accepted_key_types:
                        raise ValueError('The index key returned by the index %s is not valid' % (
                            set_.index.__class__.__name__
                        ))
                    final_sets.add(index_key)
                    if is_tmp:
                        tmp_keys.add(index_key)
            else:
                raise ValueError('Invalid filter type')

        return final_sets, tmp_keys

    def _get_final_set(self, sets, pk, sort_options):
        """
        Called by _collection to get the final set to work on. Return the name
        of the set to use, and a list of keys to delete once the collection is
        really called (in case of a computed set based on multiple ones)
        """
        conn = self.cls.get_connection()
        all_sets = set()
        tmp_keys = set()

        if pk is not None and not sets and not (sort_options and sort_options.get('get')):
            # no final set if only a pk without values to retrieve
            return (None, False)

        elif sets or pk:
            if sets:
                new_sets, new_tmp_keys = self._prepare_sets(sets)
                all_sets.update(new_sets)
                tmp_keys.update(new_tmp_keys)
            if pk is not None:
                # create a set with the pk to do intersection (and to pass it to
                # the store command to retrieve values if needed)
                tmp_key = self._unique_key()
                conn.sadd(tmp_key, pk)
                all_sets.add(tmp_key)
                tmp_keys.add(tmp_key)

        else:
            # no sets or pk, use the whole collection instead
            all_sets.add(self.cls.get_field('pk').collection_key)

        if len(all_sets) == 1:
            # if we have only one set, we  delete the set after calling
            # collection only if it's a temporary one, and we do not delete
            # it right now
            final_set = all_sets.pop()
            if final_set in tmp_keys:
                delete_set_later = True
                tmp_keys.remove(final_set)
            else:
                delete_set_later = False
        else:
            # more than one set, do an intersection on all of them in a new key
            # that will must be deleted once the collection is called.
            delete_set_later = True
            final_set = self._combine_sets(all_sets, self._unique_key())

        if tmp_keys:
            conn.delete(*tmp_keys)

        # return the final set to work on, and a flag if we later need to delete it
        return (final_set, [final_set] if delete_set_later else None)

    def _combine_sets(self, sets, final_set):
        """
        Given a list of set, combine them to create the final set that will be
        used to make the final redis call.
        """
        self.cls.get_connection().sinterstore(final_set, list(sets))
        return final_set

    def __call__(self, **filters):
        return self._add_filters(**filters)

    def _field_is_pk(self, field_name):
        """Check if the given name is the pk field, suffixed or not with "__eq" """
        if self.cls._field_is_pk(field_name):
            return True
        if field_name.endswith('__eq') and self.cls._field_is_pk(field_name[:-4]):
            return True
        return False

    def _parse_filter_key(self, key):
        # Each key can have optional subpath
        # We pass it as args to the field, which is responsable
        # from handling them
        # We only manage here the suffix handled by a filter

        key_path = key.split('__')
        field_name = key_path.pop(0)
        field = self.cls.get_field(field_name)

        if not field.indexable:
            raise ImplementationError(
                'Field %s.%s is not indexable' % (
                    field._model.__name__, field.name
                )
            )

        other_field_parts = key_path[:field._field_parts - 1]

        if len(other_field_parts) + 1 != field._field_parts:
            raise ImplementationError(
                'Unexpected number of parts in filter %s for field %s.%s' % (
                    key, field._model.__name__, field.name
                )
            )

        rest = key_path[field._field_parts - 1:]
        index_suffix = None if not rest else '__'.join(rest)
        index_to_use = None
        for index in field._indexes:
            if index.can_handle_suffix(index_suffix):
                index_to_use = index
                break

        if not index_to_use:
            raise ImplementationError(
                'No index found to manage filter %s for field %s.%s' % (
                    key, field._model.__name__, field.name
                )
            )

        return index_to_use, index_suffix, other_field_parts

    def _add_filters(self, **filters):
        """Define self._lazy_collection according to filters."""
        for key, value in filters.items():
            if self._field_is_pk(key):
                pk = self.cls.get_field('pk').normalize(value)
                self._lazy_collection['pks'].add(pk)
            else:
                # store the info to call the index later, in ``_prepare_sets``
                # (to avoid doing extra work if the collection is never called)
                index, suffix, extra_field_parts = self._parse_filter_key(key)
                parsed_filter = ParsedFilter(index, suffix, extra_field_parts, value)
                self._lazy_collection['sets'].append(parsed_filter)

        return self

    def __len__(self):
        if self._len is None:
            if not self._len_mode:
                self._len = self._collection.__len__()
            else:
                self._collection
        return self._len

    def __repr__(self):
        old_slice_and_len_mode = None if self._slice is None else self._slice.copy(), self._len_mode
        try:
            self._len_mode = False
            return self._collection.__repr__()
        finally:
            self._slice, self._len_mode = old_slice_and_len_mode

    def instances(self, skip_exist_test=False):
        """
        Ask the collection to return a list of instances.
        If skip_exist_test is set to True, the instances returned by the
        collection won't have their primary key checked for existence.
        """
        self.reset_result_type()
        self._instances = True
        self._instances_skip_exist_test = skip_exist_test
        return self

    def _get_simple_fields(self):
        """
        Return a list of the names of all fields that handle simple values
        (StringField or InstanceHashField), that redis can use to return values via
        the sort command (so, exclude all fields based on MultiValuesField)
        """
        fields = []
        for field_name in self.cls._fields:
            field = self.cls.get_field(field_name)
            if not isinstance(field, MultiValuesField):
                fields.append(field_name)
        return fields

    def primary_keys(self):
        """
        Ask the collection to return a list of primary keys. It's the default
        but if `instances`, `values` or `values_list` was previously called,
        a call to `primary_keys` restore this default behaviour.
        """
        self.reset_result_type()
        return self

    def reset_result_type(self):
        """
        Reset the type of values attened for the collection (ie cancel a
        previous "instances" call)
        """
        self._instances = False

    def _coerce_by_parameter(self, parameters):
        if "by" in parameters:
            by = parameters['by']
            # Manage desc option
            if by.startswith('-'):
                parameters['desc'] = True
                by = by[1:]
            if self.cls._field_is_pk(by):
                # don't use field, the final set, which contains pks, will be sorted directly
                del parameters['by']
            elif self.cls.has_field(by):
                # if we have a field, use its redis wildcard form
                field = self.cls.get_field(by)
                parameters['by'] = field.sort_wildcard
        return parameters

    def sort(self, **parameters):
        """
        Parameters:
        `by`: pass either a field name or a wildcard string to sort on
              prefix with `-` to make a desc sort.
        `alpha`: set it to True to sort lexicographically instead of numerically.
        """
        parameters = self._coerce_by_parameter(parameters)
        self._sort = parameters
        return self

    def _unique_key(self):
        """
        Create a unique key.
        """
        return unique_key(self.cls.get_connection())
