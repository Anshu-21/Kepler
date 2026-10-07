# Facility settlement specification

A facility has several tranches on one payment schedule. On each payment date the desk works out what every tranche is owed and distributes that date's collections through the priority of payments; the result is that date's statement. This document defines settlement exactly, and the last sections define the reconstruction you are asked to build on top of it.

Everything settlement needs is in the contract file: each tranche's day count, rate terms, seniority and withholding, the reserve account, the calendars, the published SOFR fixings, the prepayments, the rate bookings and the cash collected for each payment date.

## Schedule

Regular boundary `k` (for `k = 1 .. term_months`) is `opening_date` moved forward `k` months onto `anchor_day`, or onto the last day of the month when that month is shorter. Each boundary is computed from the opening date, never from the previous boundary.

A payment business day is a weekday not listed in `payment_holidays`. Modified Following moves a date forward to the next payment business day, unless that crosses into the next month, in which case it moves backward to the previous payment business day instead.

- Accrual start of period 1 is `opening_date`, never adjusted. Accrual start of period `k > 1` is the accrual end of period `k - 1`.
- Accrual end is the regular boundary when `accrual_dates` is `UNADJUSTED`, and its Modified Following date when it is `ADJUSTED`.
- Payment date is always the Modified Following date of the regular boundary.
- Determination date is the payment date moved back `notice_days` payment business days.

Effective dates of prepayments and rate changes fall strictly inside a period, at least five calendar days from every regular boundary.

## Prepayments

`prepayments` is cash that was actually received. Each one reduces its tranche's balance from its `effective_date`, capped at the balance. Prepayments are never corrected.

## Rate bookings and what is known when

`bookings` is an append-only log of `RATE_CHANGE` and `FIXING_CORRECTION` records. Every record has `booking_id`, `logical_id`, `revision`, `recorded_at` and `action`. `recorded_at` is an ISO 8601 timestamp with a UTC offset (`Z` or `±HH:MM`); the desk records from several offices, so the offsets vary. Revisions of one logical booking are distinct integers. `SET` records carry `kind` and its fields; `RETRACT` records carry nothing else.

Period `k` is settled with what is known at the close of its determination date, 17:00 New York time on that date: only records whose `recorded_at` is at or before that instant exist. New York is at UTC-04:00 from the second Sunday of March up to, but not including, the first Sunday of November, and at UTC-05:00 otherwise. For each `logical_id` the record with the highest `revision` among those wins; a winning `RETRACT` means the booking does not exist. `booking_id`, log order and recording order carry no meaning.

- `RATE_CHANGE`: from `effective_date`, `annual_rate` replaces a fixed tranche's rate, or `spread` replaces a floating tranche's spread.
- `FIXING_CORRECTION`: `fixing` replaces the published fixing for `fixing_date`. No two logical corrections share a date.

## Interest accrual

A tranche accrues over its accrual period in segments. Its segments break at the effective date of each of its own prepayments and known rate changes, and nowhere else. Several of these on one date apply in ascending `logical_id` order. A change takes effect at the start of its date: the segment before it accrues with the old balance and rate.

A segment `[a, b)` accrues `balance * rate * year_fraction(a, b)`, rounded half up to the cent. A period's accrued interest is the sum of its segments. Interest is never added to the balance.

Fixed tranches use `annual_rate`. Floating tranches use `max(compounded, floor) + spread`. A tranche that ended the previous period with `deferred_interest` above zero is in default for this whole period and adds `default_margin` to its rate in every segment, after the floor. `compounded` is daily compounded SOFR in arrears with a lookback, computed for that segment alone:

- A rate business day is a weekday not listed in `rate_holidays`. This is a different calendar from the payment calendar.
- Every calendar day `t` in `[a, b)` belongs to the latest rate business day on or before `t` (which may fall before `a`). Days belonging to the same rate business day `i` form one run of `n_i` days.
- The fixing for that run is the one for the rate business day `lookback_days` rate business days before `i`. `fixings` maps ISO dates to percentages: `"4.53"` means 0.0453.
- A floating tranche may have `lockout_days` `L`. Its lockout days in a period are the last `L` rate business days before that period's accrual end. A run whose rate business day is a lockout day takes the fixing of the rate business day just before the first lockout day instead, with the lookback applied from there. This is fixed by the period, so a segment that ends before the period does is still locked out on any lockout days it contains. Without the field there is no lockout.
- `compounded = (product over runs of (1 + fixing * n_i / 360) - 1) * 360 / D`, with `D` the calendar days from `a` to `b`, rounded half up to seven decimal places. This 360 is the SOFR convention and does not depend on the tranche's day count.

Year fractions, with `a = Y1-M1-D1` and `b = Y2-M2-D2`:

- `ACT/360`: actual days / 360. `ACT/365F`: actual days / 365.
- `ACT/ACT ISDA`: days falling in each calendar year divided by that year's length (366 in a leap year, else 365), summed.
- `ACT/ACT ICMA`: the days of `[a, b)` that fall in each regular period, divided by 12 times that regular period's length in days, summed. Regular period `k` runs from regular boundary `k - 1` to regular boundary `k`, with boundary 0 the opening date and boundary `term_months + 1` computed the same way. Regular periods are never adjusted, even when accrual dates are, so an adjusted accrual period or one of its segments can span two of them.
- `30/360`: `D1 = min(D1, 30)`; if `D2` is 31 and `D1` is 30, `D2 = 30`.
- `30E/360`: `D1 = min(D1, 30)`, `D2 = min(D2, 30)`.
- `30E/360 ISDA`: `D1 = 30` if `a` is the last day of its month; `D2 = 30` if `b` is the last day of its month, unless `b` is the termination date and in February. Otherwise days above 30 become 30. The termination date is the accrual end of the final period.
- The 30-day conventions then use `(360 * (Y2 - Y1) + 30 * (M2 - M1) + D2 - D1) / 360`.

Compute every product and fraction exactly before rounding. Withholding and reserve targets are products of exact decimals, and an exact half there rounds up. No other rounding in any contract, including the verifier's, lands within 1e-7 of a unit from a half, so `Decimal` in its default context reaches the same cents as exact arithmetic.

## Restatement and true-up

Balances and default status are history: a period's opening balance and whether it carries the default margin are whatever the settlements actually left, and prepayments are always known. Rates and fixings are not. When period `k` is settled, the accrued interest of every earlier period is recomputed on its actual balances with the bookings known now. A tranche's `true_up` is that restated interest for periods `1 .. k-1` minus everything recognised for it so far, where each period recognises its `interest` plus its `true_up`. It can be negative.

## Amounts due

For each tranche on payment date `k`:

- `interest` is the period's accrued interest with current knowledge.
- Current interest is `interest + true_up` plus any credit carried from the previous period. When that is negative, current interest is zero and the negative amount is carried as credit to the next period instead. Credit only offsets current interest.
- `overdue_interest` is the previous period's `deferred_interest` times `overdue_rate` times the tranche's own year fraction from the previous payment date to this one, rounded half up to the cent. It is zero in period 1.
- `interest_due` is current interest plus the previous `deferred_interest` plus `overdue_interest`.
- `principal_due` is `scheduled_principal` plus the previous period's unpaid principal, capped at the balance after this period's prepayments. In the final period it is that whole balance.

The senior fee due is `senior_fee` plus any fee left unpaid in the previous period.

## Withholding

A tranche with a `withholding_rate` has tax withheld from every interest payment made to it. The tax on a gross payment `P` is `P * withholding_rate` rounded half up to the cent, and the tranche is credited with `P` less that tax. The borrower grosses up: the gross interest due is the smallest whole-cent amount whose credit after withholding is at least `interest_due`. A tranche without the field has a rate of zero, so its gross interest due is its `interest_due`.

In the interest step each tranche's amount is its gross interest due, and pari passu sharing uses those gross amounts. `withholding` is the tax on what the tranche was paid and `interest_paid` is the credit after it, so the cash a tranche takes for interest is their sum. `deferred_interest` is `interest_due` minus `interest_paid`.

## Reserve account

`reserve` is null or an account with `opening_balance`, `target_rate`, `floor` and `covers`, a list of seniority levels.

- Draw: immediately before the interest step of a level in `covers`, if the cash left is less than that level's total gross interest due, the shortfall is drawn from the reserve, up to its balance, and joins the cash.
- Top-up: on every payment date but the last, after the principal step and before the sweep or release, the cash left tops up the reserve. The top-up is the smallest whole-cent amount `R`, no more than the cash left, after which the reserve balance is at least the target; when there is none it is all the cash left. The target is `target_rate` times the total balance of all tranches after the sweep that the cash left after `R` would make (after the principal step when `excess_cash` is `RELEASE`), rounded half up to the cent, and never less than `floor`.
- On the final payment date the whole reserve is drawn right after the senior fee, and there is no top-up.

`reserve_draw` is everything drawn on a payment date, `reserve_topup` the top-up and `reserve_balance` the balance afterwards. All three are zero for a contract without a reserve.

## Priority of payments

`collections[k-1]` is the cash available on payment date `k`; `collateral[k-1]` is the collateral balance reported for it. It is applied in this order, each step taking what it can from what is left:

1. The senior fee.
2. Interest, cures and principal, with reserve draws before covered levels' interest. If `waterfall` is `INTEREST_FIRST`: for each seniority level in ascending order, its interest due and then its coverage cure; then the principal still due of each level in ascending order. If it is `LEVEL_BY_LEVEL`: for each level in ascending order, its interest due, its coverage cure and then its principal still due.
3. The reserve top-up, when there is a reserve.
4. If `excess_cash` is `SWEEP`: for each level in ascending order, pay down the balances left after this period's principal. Whatever remains, or everything left when it is `RELEASE`, is `released`.

Tranches with the same `seniority` are one level and share it pari passu: when the cash left cannot pay the level in full, each tranche gets `cash * its amount / level total` truncated to the cent, and the whole cents still left go one each in descending order of the tranche's amount, ties broken by ascending `tranche_id`. In a sweep, the amounts are the balances.

`coverage_tests` maps some levels (as strings) to a trigger. The test of level `L` compares `collateral[k-1]` with the outstanding balance of every tranche in levels up to and including `L`, where outstanding means the balance after this period's prepayments less principal already paid in this waterfall. It passes when `outstanding * trigger <= collateral`. Otherwise the cure is `outstanding - collateral / trigger` rounded up to the cent, and it is paid from what cash is left to those levels in ascending order, pari passu by outstanding balance within a level, never more than a level's outstanding total. A cure is principal: it counts in both `cure_paid` and `principal_paid`, and the principal step later pays only what is still due, which is `principal_due` less principal already paid, floored at zero.

Afterwards `deferred_interest` is interest due minus interest paid, `principal_due` less `principal_paid` (floored at zero) carries to the next period's principal due, and the balance falls by principal paid and swept. That balance opens the next accrual period.

## Statements

Settling a contract produces its statement: exactly `contract_id` and `periods`, one entry per period in order. Each period contains exactly `number`, `accrual_start`, `accrual_end`, `payment_date`, `determination_date` (ISO dates), `collections`, `fee_paid`, `reserve_draw`, `reserve_topup`, `reserve_balance`, `released` and `tranches`. `tranches` maps every `tranche_id` to exactly `interest`, `true_up`, `overdue_interest`, `interest_due`, `interest_paid`, `withholding`, `deferred_interest`, `principal_due`, `principal_paid`, `cure_paid`, `swept`, `closing_balance` and `default_margin`, the last a boolean saying whether the period accrued with the default margin. Money values are strings with exactly two decimals and a leading `-` only when negative, such as `"1250.00"` and `"-0.07"`.

## Reconstruction

The `RATE_CHANGE` records of the booking log were lost. For each affected facility the desk still has the contract, whose `bookings` now hold only the `FIXING_CORRECTION` records (with their revisions and retractions), and the statement it issued, which was produced by settling the complete contract.

`reconstruct(contract, statement)` returns a list of `RATE_CHANGE` records such that settling the contract with them added to its `bookings` reproduces `statement` exactly, in every field of every period. Any such list is accepted; it does not have to be the one that was lost. Every returned record must be a valid booking under this specification:

- `logical_id` is a string not used by any record already in the contract, `revision` is an integer, and the revisions of one `logical_id` are distinct.
- `recorded_at` is an ISO 8601 timestamp with a UTC offset, and `action` is `SET` or `RETRACT`. `booking_id` may be omitted.
- A `SET` record has `kind` `RATE_CHANGE`, a `tranche_id` of the contract, an `effective_date` strictly inside a period and at least five calendar days from every regular boundary, and `annual_rate` for a fixed tranche or `spread` for a floating one, as a decimal string with at most eight decimal places.
- A `RETRACT` record has nothing else.
- There are at most 5,000 records.

The input must not be modified.

## Bounds

Every contract, including the verifier's, has two to six tranches, `term_months` between 6 and 120, `anchor_day` between 1 and 31, an `opening_date` that is a payment business day, `notice_days` between 1 and 5, `lookback_days` between 2 and 5, `lockout_days` (when present) between 1 and 3, withholding rates below 0.5, at most 2,000 bookings and 40 prepayments, and fixings for every date a replay looks up.

## Runtime

`reconstruct(contract, statement)` gets 120 seconds of wall clock per facility, in a fresh single-threaded Python process.
