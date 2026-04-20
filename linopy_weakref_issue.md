# Add `__weakref__` to `Model.__slots__`

`linopy.Model` declares `__slots__` but does not include `__weakref__`, which prevents any external registry from tracking `Model` instances.

```python
import linopy, weakref
m = linopy.Model()
weakref.ref(m)
# TypeError: cannot create weak reference to 'Model' object
```

## Use case

Third-party extensions (e.g. accessor-style libraries that add `m.foo.<method>()`) need per-instance storage without polluting `Model`'s own slots or adding `__dict__`. The standard Python pattern is a `WeakKeyDictionary` keyed by the instance, so entries are garbage-collected with the model. That requires the class to be weakref-able.

## Proposed change

```python
class Model:
    __slots__ = (
        "_variables", "_constraints", ...,
        "__weakref__",   # add
    )
```

## Trade-offs

- Adds one pointer per instance (~8 bytes). No behavioral change.
- No API commitment or new public surface.
- Enables external extension patterns without requiring users to subclass `Model`.

## Compatibility

The change is strictly additive. Existing code that does not touch `weakref` is unaffected. Pickling and `copy`/`deepcopy` are not impacted — `__weakref__` is a runtime-only slot and is not serialized.

One narrow case to flag: any downstream subclass of `linopy.Model` that already declares `__weakref__` in its own `__slots__` (to make that subclass weakref-able) would break at import with:

```
TypeError: __weakref__ slot disallowed: either we already got one, or __itemsize__ != 0
```

The fix on the subclass side is a one-line removal of the redundant `__weakref__` entry. This is unlikely to be common in the wild, but worth noting in release notes.
