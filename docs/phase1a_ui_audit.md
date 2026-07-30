# Phase 1A UI Audit

## Scope and guardrails

This audit covers user-facing information architecture and presentation only. Commission formulas, unit-credit behavior, rule selection, effective dates, activation, sandbox isolation, ownership, parsing, and stored data remain authoritative and unchanged.

## View Sales

- **Primary goal:** Add, inspect, edit, and delete deals for a selected month.
- **Previously shown:** Deal table, per-deal commission, and a fixed summary repeating front-end, back-end, bonus, and total values from View Commission.
- **Duplication/conflict:** The full monthly earnings breakdown competed with the deal-management task and duplicated the Commission page.
- **Readability/accessibility:** “Count” did not explain unit credit; action columns lacked scoped table headers; the fixed summary obscured content at small widths.
- **Decision:** Keep deal and per-deal calculation access. Rename Count to Unit credit. Consolidate the month into one concise line containing unit credit and estimated total. Move the detailed earnings breakdown exclusively to View Commission. Use a purposeful empty state instead of zero totals.

## View Commission

- **Primary goal:** Provide the authoritative estimated earnings summary and calculation transparency.
- **Currently shown:** Engine-backed totals, active-plan context, bonus progress, draw progress, sale diagnostics, and calculation explanations.
- **Duplication/conflict:** “Sales commission” and “Finance commission” conflicted with front-end/back-end terminology. Adjustments were available in context but omitted from the main total cards.
- **Decision:** Retain the authoritative `sales_month_context` values. Standardize labels, add adjustments, keep explanations under “How this was calculated,” and replace empty zero cards with “Not available yet.”

## Pay Plan Setup and Review

- **Primary goal:** Supply a plan, inspect the interpretation, and knowingly activate it.
- **Currently shown:** Source material, parser metadata, extracted rules, warnings, and activation controls.
- **Technical content:** Parser confidence and extracted engine identifiers are valuable during approval but should not be the first description of a rule.
- **Decision:** Preserve parser warnings, source, approval state, and activation consequences. A later focused pass can apply the same human-readable rule component to dictionary-based import drafts after their display contract is normalized.

## Pay Plan Rules

- **Primary goal:** Understand what the active or historical plan pays and when it applies.
- **Previously shown:** `rule_type`, calculation scope, priority, raw configuration dictionaries, and raw condition identifiers as primary content.
- **Decision:** Lead with readable summaries such as “25% of front-end gross” and “$500 bonus at 10 units.” Translate common conditions. Move rule type, scope, and configuration into collapsed Advanced details. Keep editing reachable and ownership filtering unchanged.

## Pay Plan Assistant

- **Primary goal:** Request, review, and confirm a draft change.
- **Previously shown:** Correct safety statement, effective-date form, examples, and recent requests with unstyled statuses.
- **Decision:** Clarify the four-step draft workflow and effective-date consequences, keep field-level form errors, and use reusable human-readable status badges. The active plan remains unchanged until explicit activation.

## Commission Sandbox and scenario comparison

- **Primary goal:** Privately simulate and compare alternative plan rules.
- **Previously shown:** Strong isolation text, but it was repeated with differing wording and could be missed on nested pages.
- **Decision:** Add one consistent, prominent simulation banner to index, comparison, detail, and rule-edit pages. Preserve all scenario actions and conversion safeguards. Draft conversion remains distinct from active-plan activation.

## Activity and Goals

- **Primary goal:** Record daily activity, set monthly goals, and review progress.
- **Duplication:** Activity history and printable reports intentionally repeat data for distinct working and reporting contexts.
- **Decision:** Keep current data and report actions. Future work may extract its metric cards after chart and print parity are verified.

## Reports

- **Primary goal:** Produce stable printable records.
- **Technical/duplicate content:** Repetition with interactive pages is intentional because print views are a separate output responsibility.
- **Decision:** Keep calculations and printable detail unchanged. Do not reuse interactive controls in print templates.

## Header and navigation

- **Primary goal:** Reach Sales, Commission, Activity & Goals, and account settings consistently.
- **Finding:** The authenticated base navigation contains one instance of each primary destination. An older `app_header.html` belongs to a separate legacy template path and is not included by the current base.
- **Decision:** Preserve the three unique primary destinations and theme/header-color behavior. Pay-plan and sandbox tools remain contextual under Commission rather than becoming duplicate global links.

## Empty, warning, validation, and error states

- **Finding:** Empty Sales and Commission pages could imply calculated `$0.00`; warning language varied; Django field errors already preserve bound form data.
- **Decision:** Use a reusable empty-state component, “Needs attention” for actionable calculation gaps, and retain form-bound values and field-level errors.

## Shared presentation decisions

- Reusable components: page header, status badge, empty state, commission totals, rule summary, and sandbox banner.
- Currency is two decimal places; percentages are humanized; unit thresholds preserve whole and half units.
- Raw engine configuration remains available only under Advanced details.
- Mobile tables intentionally scroll horizontally; page-level overflow is constrained.
- Existing user-selected themes and header colors remain untouched.

## Deferred improvements

- Normalize import-draft dictionaries into a typed presentation contract so setup review can reuse the rule-summary component without template branching.
- Consolidate legacy and V2 Commission views after legacy retirement is explicitly approved.
- Move remaining inline base CSS into the static stylesheet in a separate asset-caching change.
- Replace page-local dialog scripts with one tested shared module after browser automation is available.
