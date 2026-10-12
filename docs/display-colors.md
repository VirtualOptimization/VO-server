# Shared Placeholder Colors

Every furniture entry in `objects` includes `display_color_hex` (`#RRGGBB`).
RoomPlan and Unity layouts use the same color for the same `identifier`.
Clients must use this value only for OBB/placeholder rendering when a catalog
model is unavailable, never to override USDZ/GLB model materials.

| Category | Default |
| --- | --- |
| bed | #F2CCD4 |
| sofa, chair | #B8D4E6 |
| table | #E0D1B8 |
| storage | #C7D1C2 |
| television | #8C8C94 |
| refrigerator | #E0E6EB |
| washerdryer | #D1E0EB |
| other | #CCC7BF |

Valid stored colors are retained and normalized to uppercase. Missing or invalid
values receive the category default. On download endpoints, legacy S3 layouts
are enriched before presigned URLs are returned. RoomPlan is authoritative when
paired layouts have inconsistent colors. This requires S3 read/write access.
No rescan or database schema migration is needed. Previously issued URLs are
not proactively migrated; request the room/version again to trigger backfill.
