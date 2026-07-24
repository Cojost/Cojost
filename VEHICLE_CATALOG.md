# Vehicle catalog

`python manage.py sync_vehicle_catalog` imports passenger-vehicle makes and models
from the official NHTSA vPIC API into the local database. Use `--dry-run` to
preview without saving, or `--make "Subaru"` to restrict synchronization.

Normal sale entry never calls NHTSA. If vPIC is unavailable, the existing local
catalog and explicitly confirmed custom entries remain usable. vPIC coverage and
naming reflect the source dataset and may include historical or uncommon models;
administrators can review entries using the verified and active fields.
