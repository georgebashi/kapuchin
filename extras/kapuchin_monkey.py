# -*- coding: utf-8 -*-
"""
Convenient approach to monkey patching.

This is a Python 3–only, inlined, minimal adaptation of the 'gorilla' library
tailored for Kapuchin. It always allows overwriting existing attributes and
always stores originals (equivalent to allow_hit=True and store_hit=True).
The API surface preserved here is sufficient for our use:

- default_filter
- DecoratorData
- Patch
- apply, revert
- patch, patches
- destination, name, filter
- create_patches, find_patches
- get_attribute, get_original_attribute, get_decorator_data

All internal marker attributes are renamed to use the '_monkey_*' prefix.
"""

__all__ = [
    'default_filter', 'DecoratorData', 'Patch', 'apply', 'revert',
    'patch', 'patches', 'destination', 'name', 'filter',
    'create_patches', 'find_patches', 'get_attribute',
    'get_original_attribute', 'get_decorator_data'
]

__title__ = 'monkey'
__version__ = '0.1.0-inline'
__summary__ = "Lightweight inline monkey patching (based on gorilla)"
__url__ = 'https://github.com/christophercrouzet/gorilla'
__author__ = "Kapuchin maintainers"
__license__ = "MIT"

import collections
import copy
import inspect
import importlib
import pkgutil
import sys
import types

_CLASS_TYPES = (type,)

def _iteritems(d):
    return iter(d.items())

# Pattern for each internal attribute name.
_PATTERN = '_monkey_{}'

# Pattern for the flag expressing whether an attribute was created.
_CREATED = _PATTERN.format('created_{}')

# Pattern for the ids of the original attributes stored.
_ORIGINAL_IDS = _PATTERN.format('ids_{}')

# Pattern for each original attribute stored.
_ORIGINAL_ITEM = _PATTERN.format('item_{}_{}')

# Attribute for the decorator data.
_DECORATOR_DATA = _PATTERN.format('decorator_data')


def default_filter(name, obj):
    """Attribute filter.

    It filters out module attributes, and also methods starting with an
    underscore '_'.

    Used as the default filter for create_patches() and patches().
    """
    return not (isinstance(obj, types.ModuleType) or name.startswith('_'))


class DecoratorData(object):
    """Decorator data.

    Attributes
    ----------
    patches : list of monkey.Patch
        Patches created through the decorators.
    override : dict
        Any overriding value defined by the destination(), name(), and filter()
        decorators.
    filter : bool or None
        Value defined by the filter() decorator, if any, or None otherwise.
    """

    def __init__(self):
        self.patches = []
        self.override = {}
        self.filter = None


class Patch(object):
    """Describe all the information required to apply a patch.

    Attributes
    ----------
    destination : obj
        Patch destination.
    name : str
        Name of the attribute at the destination.
    obj : obj
        Attribute value.
    settings : object or None
        Ignored. Kept for compatibility with upstream call sites.
    """

    def __init__(self, destination, name, obj, settings=None):
        self.destination = destination
        self.name = name
        self.obj = obj
        self.settings = settings  # ignored, kept for compatibility

    def __repr__(self):
        return (
            '{}(destination={!r}, name={!r}, obj={!r})'
            .format(type(self).__name__, self.destination, self.name, self.obj)
        )

    def __hash__(self):
        return hash(sorted(_iteritems(self.__dict__)))

    def __eq__(self, other):
        if isinstance(other, type(self)):
            return self.__dict__ == other.__dict__
        return NotImplemented

    def __ne__(self, other):
        is_equal = self.__eq__(other)
        return is_equal if is_equal is NotImplemented else not is_equal

    def _update(self, **kwargs):
        """Update some attributes (used by modifier decorators)."""
        for key, value in _iteritems(kwargs):
            setattr(self, key, copy.deepcopy(value))


def apply(patch, id='default'):
    """Apply a patch.

    The patch's obj is injected into the patch's destination under the
    patch's name.

    Always allows overwriting and always stores the original on hit.
    """
    # When a hit occurs due to an attribute at the destination already existing
    # with the patch's name, the existing attribute is referred to as 'target'.
    try:
        target = get_attribute(patch.destination, patch.name)
    except AttributeError:
        created = _CREATED.format(patch.name)
        setattr(patch.destination, created, True)
    else:
        # Always store original attribute under a different name before overriding.
        original_ids = _ORIGINAL_IDS.format(patch.name)
        ids = getattr(patch.destination, original_ids, ())

        original_item = _ORIGINAL_ITEM.format(patch.name, len(ids))
        setattr(patch.destination, original_item, target)

        ids += (id,)
        setattr(patch.destination, original_ids, ids)

    setattr(patch.destination, patch.name, patch.obj)


def revert(patch):
    """Revert a patch.

    This is only possible if the attribute existed before the patch was applied
    (or if it was created by the patch, in which case it will be deleted).
    """
    created = _CREATED.format(patch.name)
    if getattr(patch.destination, created, False):
        delattr(patch.destination, patch.name)
        return

    original_ids = _ORIGINAL_IDS.format(patch.name)
    try:
        ids = getattr(patch.destination, original_ids)
        if not ids:
            raise AttributeError
    except AttributeError:
        raise RuntimeError(
            "Cannot revert the attribute named '{}' because no original was stored."
            .format(patch.destination.__name__))

    original_item = _ORIGINAL_ITEM.format(patch.name, len(ids) - 1)
    attr = getattr(patch.destination, original_item)
    setattr(patch.destination, patch.name, attr)
    delattr(patch.destination, original_item)
    setattr(patch.destination, original_ids, ids[:-1])


def patch(destination, name=None, settings=None):
    """Decorator to create a patch.

    The object being decorated becomes the Patch.obj attribute of the patch.
    """
    def decorator(wrapped):
        base = _get_base(wrapped)
        name_ = base.__name__ if name is None else name
        patch_obj = Patch(destination, name_, wrapped, settings=copy.deepcopy(settings))
        data = get_decorator_data(base, set_default=True)
        data.patches.append(patch_obj)
        return wrapped
    return decorator


def patches(destination, settings=None, traverse_bases=True,
            filter=default_filter, recursive=True, use_decorators=True):
    """Decorator to create a patch for each member of a module or a class."""
    def decorator(wrapped):
        patches_list = create_patches(
            destination, wrapped, settings=copy.deepcopy(settings),
            traverse_bases=traverse_bases, filter=filter, recursive=recursive,
            use_decorators=use_decorators)
        data = get_decorator_data(_get_base(wrapped), set_default=True)
        data.patches.extend(patches_list)
        return wrapped
    return decorator


def destination(value):
    """Modifier decorator to update a patch's destination."""
    def decorator(wrapped):
        data = get_decorator_data(_get_base(wrapped), set_default=True)
        data.override['destination'] = value
        return wrapped
    return decorator


def name(value):
    """Modifier decorator to update a patch's name."""
    def decorator(wrapped):
        data = get_decorator_data(_get_base(wrapped), set_default=True)
        data.override['name'] = value
        return wrapped
    return decorator


def filter(value):
    """Modifier decorator to force the inclusion or exclusion of an attribute.

    value: True to force inclusion, False to force exclusion, and None to
    inherit from the behaviour defined by create_patches() or patches().
    """
    def decorator(wrapped):
        data = get_decorator_data(_get_base(wrapped), set_default=True)
        data.filter = value
        return wrapped
    return decorator


def create_patches(destination, root, settings=None, traverse_bases=True,
                   filter=default_filter, recursive=True, use_decorators=True):
    """Create a patch for each member of a module or a class."""
    if filter is None:
        filter = _true

    out = []
    root_patch = Patch(destination, '', root, settings=settings)
    stack = collections.deque((root_patch,))
    while stack:
        parent_patch = stack.popleft()
        members = _get_members(parent_patch.obj, traverse_bases=traverse_bases,
                               filter=None, recursive=False)
        for name_, value in members:
            patch_obj = Patch(parent_patch.destination, name_, value,
                              settings=copy.deepcopy(parent_patch.settings))
            if use_decorators:
                base = _get_base(value)
                decorator_data = get_decorator_data(base)
                filter_override = (None if decorator_data is None
                                   else decorator_data.filter)
                if ((filter_override is None and not filter(name_, value))
                        or filter_override is False):
                    continue

                if decorator_data is not None:
                    patch_obj._update(**decorator_data.override)
            elif not filter(name_, value):
                continue

            if recursive and isinstance(value, _CLASS_TYPES):
                try:
                    target = get_attribute(patch_obj.destination, patch_obj.name)
                except AttributeError:
                    pass
                else:
                    if isinstance(target, _CLASS_TYPES):
                        patch_obj.destination = target
                        stack.append(patch_obj)
                        continue

            out.append(patch_obj)

    return out


def find_patches(modules, recursive=True):
    """Find all the patches created through decorators."""
    out = []
    modules_iter = (module
                    for package in modules
                    for module in _module_iterator(package, recursive=recursive))
    for module in modules_iter:
        members = _get_members(module, filter=None)
        for _, value in members:
            base = _get_base(value)
            decorator_data = get_decorator_data(base)
            if decorator_data is None:
                continue
            out.extend(decorator_data.patches)
    return out


def get_attribute(obj, name):
    """Retrieve an attribute while bypassing the descriptor protocol."""
    objs = inspect.getmro(obj) if isinstance(obj, _CLASS_TYPES) else [obj]
    for obj_ in objs:
        try:
            return object.__getattribute__(obj_, name)
        except AttributeError:
            pass
    raise AttributeError("'{}' object has no attribute '{}'"
                         .format(type(obj), name))


def get_original_attribute(obj, name, id='default'):
    """Retrieve an overriden attribute that has been stored."""
    original_ids = _ORIGINAL_IDS.format(name)
    try:
        ids = getattr(obj, original_ids)
        if not ids:
            raise AttributeError
    except AttributeError:
        raise AttributeError(
            "Cannot retrieve the attribute named '{}' because no original was stored."
            .format(obj.__name__))

    for i, original_id in reversed(tuple(enumerate(ids))):
        if original_id == id:
            original_item = _ORIGINAL_ITEM.format(name, i)
            return getattr(obj, original_item)

    raise AttributeError(
        "No original attribute found matching the id '{}'.".format(id))


def get_decorator_data(obj, set_default=False):
    """Retrieve any decorator data from an object."""
    if isinstance(obj, _CLASS_TYPES):
        datas = getattr(obj, _DECORATOR_DATA, {})
        data = datas.setdefault(obj, None)
        if data is None and set_default:
            data = DecoratorData()
            datas[obj] = data
            setattr(obj, _DECORATOR_DATA, datas)
    else:
        data = getattr(obj, _DECORATOR_DATA, None)
        if data is None and set_default:
            data = DecoratorData()
            setattr(obj, _DECORATOR_DATA, data)
    return data


def _get_base(obj):
    """Unwrap decorators to retrieve the base object."""
    if hasattr(obj, '__func__'):
        obj = obj.__func__
    elif isinstance(obj, property):
        obj = obj.fget
    elif isinstance(obj, (classmethod, staticmethod)):
        # Fallback for Python < 2.7 compat pattern (kept for completeness).
        obj = obj.__get__(None, object)
    else:
        return obj
    return _get_base(obj)


def _get_members(obj, traverse_bases=True, filter=default_filter,
                 recursive=True):
    """Retrieve the member attributes of a module or a class.

    The descriptor protocol is bypassed.
    """
    if filter is None:
        filter = _true

    out = []
    stack = collections.deque((obj,))
    while stack:
        obj = stack.popleft()
        if traverse_bases and isinstance(obj, _CLASS_TYPES):
            roots = [base for base in inspect.getmro(obj)
                     if base not in (type, object)]
        else:
            roots = [obj]

        members = []
        seen = set()
        for root in roots:
            for name, value in getattr(root, '__dict__', {}).items():
                if name not in seen and filter(name, value):
                    members.append((name, value))
                seen.add(name)

        members = sorted(members)
        for _, value in members:
            if recursive and isinstance(value, _CLASS_TYPES):
                stack.append(value)

        out.extend(members)

    return out


def _module_iterator(root, recursive=True):
    """Iterate over modules."""
    yield root

    stack = collections.deque((root,))
    while stack:
        package = stack.popleft()
        # The '__path__' attribute of a package might return a list of paths if
        # the package is referenced as a namespace.
        paths = getattr(package, '__path__', [])
        for path in paths:
            for finder, name, is_package in pkgutil.iter_modules([path]):
                module_name = '{}.{}'.format(package.__name__, name)
                module = sys.modules.get(module_name)
                if module is None:
                    module = importlib.import_module(module_name)

                if is_package:
                    if recursive:
                        stack.append(module)
                        yield module
                else:
                    yield module


def _true(*args, **kwargs):
    """Return True."""
    return True