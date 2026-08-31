# Ask Stew AI-1B contextual entry points

AI-1B makes the existing read-only Ask Stew workflow available where a user
is already reviewing a calculation. It does not add new intents, hypothetical
projections, write access, customer access, or provider permissions.

## Entry points

Authorized current-period users can prepare these existing supported prompts:

- Dashboard and Commission totals: `What have I made this month?`
- Current bonus and Vehicle Bonus Progress: `How close am I to my next bonus?`
- Recorded sale rows: `Break down deal #<owned deal number>.`
- Active plan: `How am I paid?`
- Monthly Eligibility: `What eligibility information am I missing?`

Ask Stew displays the prepared question for review. Opening a contextual link
never submits the question and never calls the provider.

## Safety boundaries

- Prompt and source identifiers are server allowlisted. Arbitrary query-string
  text is not copied into the question field.
- A deal prompt is prepared only when the deal number belongs to the signed-in
  user. The normal owner-scoped explanation query runs again after submission.
- Contextual buttons use the existing Ask Stew pilot entitlement and are absent
  for unauthorized accounts.
- Current-month prompts are not shown on historical Dashboard or Monthly
  Eligibility pages because AI-1A explanations use the current period.
- Source return links use fixed local routes; user-supplied redirect URLs are
  never accepted.
- Existing mutation, projection, privacy, prompt-injection, throttling, quota,
  and deterministic-calculation controls are unchanged.

Hypothetical questions continue to be declined before any provider call. A
future sandbox-aware phase must keep projections isolated from recorded sales,
active pay plans, and payroll totals.
