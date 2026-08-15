# Archived sale aggregation policy

`Sale` is the live record and `ArchivedSale` is prior-history storage. Monthly
Activity & Goals aggregation reads both models with the requesting user and
month on every query. `archive_sale()` preserves `(user, sale_type,
dealNumber)` and removes the live row in one transaction, so that tuple is the
stable lifecycle identity used to suppress malformed live/archive overlap. A
live row wins if overlap is present. Customer, vehicle, and date guesses are
not used for identity.

Units use recorded `count` exactly (`0.5`, `1.0`, or `2.0`). Gross is recorded
front-end plus recorded back-end and is never multiplied by `count`.

The archive schema has no stored commission payout snapshot. Legacy archive
commission is therefore unavailable: reporting returns `commission=None` plus
an incomplete diagnostic rather than recalculating with current legacy
settings or presenting `$0.00`.

Pay-plan V2 archive commission is calculated only when all of the following
historical evidence is present:

- an owner-scoped, effective-dated assignment covers the sale date;
- the assignment references the same user's protected plan version;
- the version is active or retained as inactive history;
- the version was activated no later than the archive date and its active
  rules were not updated after that date;
- every archive or monthly-eligibility input required by those rules exists;
- strict period and per-sale engine diagnostics complete successfully.

The existing V2 engine then evaluates the combined live/archive month once, so
half-deal commission, normal double-count payout, and period bonuses retain the
live engine's arithmetic without duplicate period awards. If any prerequisite
is missing, the whole archived commission total is explicitly incomplete.

This repair does not change deletion, create or backfill archive data, add a
projection, or migrate the schema.
