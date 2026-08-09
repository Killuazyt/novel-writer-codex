# Optional manual tag reference

Normal chapter writing uses plain text; the data Agent extracts facts after writing. These tags are only an optional manual annotation format and are not required by Query.

| Tag | Purpose | Required identifiers |
|---|---|---|
| `<entity>` | Declare an entity | `type`, `name`; stable `id` recommended |
| `<entity-alias>` | Register a name or title | `id` or unambiguous `ref`, plus `alias` |
| `<entity-update>` | Record a field change | `id` or unambiguous `ref`, plus a child operation |
| `<foreshadow>` | Mark an open loop | `content`, `tier` |
| `<relationship>` | Mark a relationship | two stable entity IDs and `type` |

Entity types are 角色, 地点, 物品, 势力, or 招式. If a name maps to more than one ID, do not guess; use a stable ID. Tags, when used, occupy their own line and use double-quoted attributes. Query never writes or repairs tags.
