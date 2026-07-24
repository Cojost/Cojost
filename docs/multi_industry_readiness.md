# Multi-industry readiness

SalesLogApp currently supports one internal sale type: `automotive`. The beta does
not display a sale-type selector. `Sale.sale_type` and
`ArchivedSale.sale_type` store the discriminator and default existing and new
records to `automotive`.

Automotive information remains in the typed one-to-one `Vehicle` model rather
than the universal `Sale` table. Makes and models use normalized catalog models.
The automotive form, table summary, detail dialog, and printed summary live in
`templates/sale_details/`.

`sale_types.py` contains the deliberately small extension point. Its
`AutomotiveSaleHandler` selects the detail form and template names, saves
vehicle details, and snapshots them during atomic archiving. A future supported
sale type should:

1. Add an explicit `SaleType` choice and database migration.
2. Add a typed one-to-one detail model with fields suitable for reporting.
3. Add its detail form, validation, templates, and archive snapshot.
4. Register a small handler and add authorization-aware tests and reports.

Reportable details should use explicit typed models and indexes. A giant JSON
field would weaken validation, indexing, migrations, and reliable aggregation.
Generic foreign keys and unused cross-industry columns are intentionally
avoided.

Commission calculations remain the current automotive implementation and are
not generalized by this architecture task. Organization or franchise sharing
will require separate organization, membership, role, and reporting-permission
models; `sale_type` does not grant cross-user access.
