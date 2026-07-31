# Pay Plan Intent Driver V2

## Purpose

The intent driver converts a user’s plain-language request into a structured,
reviewable proposal. Interpretation is deliberately separate from database
mutation:

1. Normalize language.
2. Interpret a structured intent without database access.
3. Resolve candidate rules from the authenticated user’s active plan.
4. Ask for missing or ambiguous information, or show an interpretation review.
5. Clone and modify an inactive draft only after the user selects **Create
   draft**.
6. Keep the existing replacement review and explicit activation workflow as a
   second safety boundary.

No conversational provider is configured and no external dependency,
credential, or paid service was added.

## Audit of the previous interpreter

The previous `create_plain_text_change_draft()` was a single atomic procedure
that combined:

- active-assignment lookup;
- draft cloning;
- regular-expression matching;
- candidate selection;
- rule mutation;
- commission preview; and
- `PayPlanChangeRequest` persistence.

It cloned before it knew whether interpretation would succeed. Transaction
rollback prevented orphan drafts after an exception, but there was no
side-effect-free interpretation or pre-draft review.

### Previous recognition paths

The old module recognized only:

- changing an existing volume-tier amount with one rigid
  `at/for … units … to/=` sentence shape;
- adding a volume tier through two amount/threshold regular expressions;
- front/back percentages when `front`, `front-end`, `finance`, or `back-end`
  appeared before `to` or `=` and a numeric percent;
- adding or removing a small fixed set of requirements from volume rules.

`PayPlanChangePattern` stored examples after successful activation but was not
read during interpretation.

### Previous limitations

- `change front minimum to 300` matched no path.
- Written numbers, currency variants, punctuation, common misspellings,
  minimums, maximums, packs, draws, and most target concepts were unsupported.
- Synonyms were duplicated among regex constants, substring checks, name
  heuristics, and the separate import parser.
- Candidate selection often changed every matching rule. Separate New and Used
  rules were not clarified.
- Missing targets, missing values, absent rules, and partial understanding
  generally reached:

  > I could not safely identify that change.

- Multiple changes in one sentence could be applied silently.
- The Assistant page created a draft on its first successful POST. The
  human-readable review appeared only after mutation.

## Intent contract

`SalesLogApp.pay_plan_intents.contract.PayPlanIntent` is an immutable dataclass
with:

- `source_text`
- `action`
- `target_type`
- `target_scope`
- `rule_selector`
- `amount`
- `percentage`
- `unit_threshold`
- `current_value`
- `new_value`
- `conditions`
- `effective_date`
- `confidence`
- `missing_information`
- `ambiguities`
- `clarification_question`
- `candidate_targets`
- `normalized_text`

Allowed actions are `add`, `change`, `remove`, `replace`, `increase`,
`decrease`, `enable`, `disable`, `rename`, and `duplicate`.

The target contract includes front/back minimum, maximum, percentage and pack;
volume tiers; flat/model/New/Used bonuses; draw; manufacturer incentive; and
condition requirements.

The deterministic interpreter produces this contract without importing Django
models or querying the database.

## Central language normalization

`normalization.py` owns:

- Unicode and capitalization normalization;
- punctuation and hyphen handling;
- comma removal inside numbers;
- written-number conversion through thousands;
- currency, percentage, and unit-threshold extraction;
- singular/plural vehicle, car, deal, sale, and unit terms;
- front/front-end/frontend/front-gross and
  back/back-end/backend/finance/F&I terms;
- minimum/min/mini/floor/guaranteed-minimum terms;
- commission/pay/payout/earnings/rate concepts; and
- the `commision` misspelling.

Amounts such as `300`, `$300`, `300 dollars`, `three hundred`, `300 bucks`, and
`$300.00` resolve to the same `Decimal('300')` value. A bare `300` in a
minimum request is currency, never 300 percent. Percentage values require a
percentage context and are stored as canonical multipliers.

## Target-handler registry

`handlers.py` maps every canonical target to a narrow handler. Handlers:

- resolve the active plan with `ActivePayPlanService`;
- derive candidates only from that authenticated user’s plan;
- derive user-facing scope labels server-side from conditions/configuration;
- return a proposal, clarification, or target-specific unsupported state;
- accept a selection only if its semantic key resolves among the current
  user-owned candidates;
- deep-copy configuration before changes;
- locate the corresponding cloned rule by server-validated semantic key; and
- modify only a `REVIEW_REQUIRED` cloned draft.

Implemented mutation handlers currently cover:

- front and back simple minimum rules;
- front and back percentage rules;
- front and back maximum rules;
- front and back pack values;
- existing flat-per-deal bonus amounts;
- existing draw amounts;
- adding or changing volume-bonus tiers; and
- adding/removing supported requirement conditions.

Model, New-vehicle, Used-vehicle, and manufacturer-incentive targets are
recognized but return a specific unsupported-operation response instead of
guessing configuration or conditions.

Simple front minimums are `minimum_commission` rules whose
`applies_to_categories` includes `front_end`. Unit-dependent
`tiered_minimum_commission` rules are intentionally not treated as one scalar
minimum.

## Clarification behavior

Interpretation returns structured missing-information and ambiguity states.
Examples:

- Missing value: `What should the new front-end minimum be?`
- Missing target: asks whether the minimum applies to front, back, or a bonus.
- Multiple rules: lists server-derived New/Used or other applicable candidates.
- No rule: asks whether the user wants to add the requested minimum; it never
  silently turns “change” into “add.”
- Multiple changes: asks whether to update them together or one at a time.
- Unsupported action/target: reports exactly what was understood and states
  that no draft was created.

The bound Django form preserves the original text, effective date, retroactive
acknowledgment, and candidate selection during clarification. No
`PayPlanChangeRequest` is stored before draft confirmation because that model
requires a draft.

## Interpretation review and confirmation

The Assistant POST workflow now has two actions:

1. `interpret` renders “Here’s what I understood,” including action, target,
   current value, new value, scope, and effective date. This path performs no
   writes.
2. `create_draft` reinterprets and re-resolves against the current active plan.
   Only then does it call the atomic mutation service.

The review offers **Create draft**, **Edit request**, and **Cancel**. The
existing replacement review remains responsible for preview review and
explicit activation.

## Validation and transaction boundary

Confirmed draft creation is one `transaction.atomic` operation:

1. Re-resolve intent and active-plan ownership.
2. Reject a stale source plan or invalid selection.
3. Deep-clone the active version with `create_manual_draft`.
4. Apply one handler.
5. Run version/rule/condition model validation.
6. Compile through `VersionAdapter` and `PayPlanCompiler`.
7. Store the canonical compilation report.
8. Run the authoritative commission-engine preview.
9. Persist `PayPlanChangeRequest`.

An exception at any point rolls back the cloned version, rules, conditions,
compilation, and request. The active version is never modified. Existing
activation validation and recalculation rollback remain unchanged.

## Provider-neutral interface

`providers.py` defines an `IntentProvider` protocol and a strict validator for
future AI-backed interpretation.

`ProviderNeutralInterpreter` is disabled unless explicitly constructed with
both a provider and `enabled=True`. It keeps deterministic recognition first
and falls back to the deterministic clarification if a provider times out or
returns invalid output.

Providers receive only source text and may return only allowlisted semantic
fields. Provider output cannot supply:

- database or rule IDs;
- trusted rule selectors;
- executable code or queries;
- arbitrary configuration JSON;
- activation instructions; or
- direct mutations.

Output action/target values, numeric fields, conditions, shape, and confidence
are validated. Low confidence, timeout, invalid shape, unknown fields, or
unallowlisted values become clarification states. Any future provider must
pass through the same authenticated server-side registry, model/domain
validation, preview, draft review, and activation workflow.

## Security and isolation

- Candidate discovery uses only the authenticated user’s active assignment.
- Active plan and owner must agree.
- Candidate names/scopes are generated from server-owned rules.
- User-supplied selections are re-resolved against current candidates.
- Another user’s semantic key cannot select a rule.
- Configuration and condition values are deep-copied during cloning.
- Conditions are preserved and never inferred during scalar changes.
- Ambiguous condition-specific rules require selection.
- Drafts remain inactive.
- Retroactive-date acknowledgment remains in `PayPlanAssistantForm`.
- Source text is displayed through Django auto-escaping and is never executed,
  parsed as template syntax, treated as SQL, or accepted as arbitrary JSON
  mutation.

## Current limitations

The driver is intentionally deterministic and bounded; it does not guarantee
every possible phrase.

- One request containing multiple changes is clarified rather than applied as
  a batch.
- Adding a brand-new simple minimum after a “change” request requires a future
  explicit add-confirmation flow.
- Unit-dependent tiered minimums require more specific tier handling.
- Model/New/Used/manufacturer bonus mutation is not yet enabled because safe
  creation requires explicit condition details.
- Backend version-level fallback fields coexist with rule-based backend
  settings; current mutation handlers operate on explicit rules and clarify
  when no applicable rule exists.
- Rename and duplicate actions are recognized but not applied by scalar
  handlers.
- No external conversational provider is enabled.
