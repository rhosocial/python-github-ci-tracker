# Architecture

Object-oriented layers with a single dependency direction:
adapters → controllers → services → models.

```
adapters (MCP / REST / CLI)   argument binding, auth, serialization, error mapping
      ↓
controllers                    one flow: trigger, query, subscribe, deliver, credentials
      ↓
services                       capabilities: the GitHub API
      ↓
models                         each model owns its data behaviour (ActiveRecord)
      ↑
PostgreSQL
```

- **Model**: owns its own data behaviour. One `save()` is one complete write.
  **There is no unit of work and no cross-model transaction.**
- **Service**: wraps a capability — today, the GitHub API. No flow, no storage.
- **Controller**: stands for one flow. The order of "read, call GitHub, store"
  lives here; a new protocol only adds an adapter.
- **App**: wiring. `container.py` builds settings / database / controllers once,
  and the adapters share that container.

`config.py`, `database.py`, `container.py` and `views.py` sit at the package root;
`adapters/` holds one App per protocol.
