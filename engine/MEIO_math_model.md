# MEIO model: mathematical formulation for Seeker (v1.3, implemented in meio_seeker_stochastic.py v18.1)

Companion to `MEIO_model_spec.md` (rule IDs in brackets), the data template (v18.1 layout) and `meio_seeker_stochastic.py`. Its
section 3 writes §3–§5 below once, as Seeker Terms (K06). `reference_simulator.py` implements the same equations a second time,
independently, for the tests.

**Changes against v1.2 (v16):**
- one fill-rate target per product, and the **expected** fill rate per cell as the rule (§5, H04/H05; the chance constraint and
  the chance level are gone);
- serving order from the input ShortagePriority (H02);
- the review period R with the **planned position** between runs (§3, A03);
- the start policy and start check (§7, K07/K21).

**Key modeling principle.** In Seeker the inventory dynamics are not constraints the solver has to satisfy, as they would be in
a MILP. They are definitional state equations: given the decisions (s,S) and a scenario ω, every state (stock, orders, sales,
waste) follows deterministically. Seeker searches over (s,S) only. The only true constraints are the fill-rate rules (expected
fill rate per channel and week ≥ target) and the variable domains. Posting the dynamics as `enforce_eq` would force the local
search to "find" feasible inventory balances, which is slow and fragile; computing them is exact and fast.

---

## 1. Sets, indices, parameters

| Symbol | Meaning | Value (template) |
|---|---|---|
| $t \in \mathcal T=\{1..H\}$ | weeks | $H=36$ |
| $\omega \in \Omega$ | scenarios (demand, lead times) | $N=$ `NumScenarios` (template 200; 500 for decisions) |
| $c \in \mathcal C$ in serving order | channels, ordered by ShortagePriority (input, H02) | D2C → B2B → AMZ |
| $k \in \mathcal K$, $\mathcal K^p\subset\mathcal K$ | raw materials; the perishable ones | RM1–RM4; RM3, RM4 |
| $a$ | remaining shelf life of a lot (weeks) | |
| $SL^{FG}, SL^k$ | shelf life at arrival | 36; RM3/RM4 52; RM1/RM2 ∞ |
| $\rho_c$, $\rho^{min}=\min_c\rho_c$ | minimum remaining shelf life to ship to $c$ | AMZ 24, D2C 16, B2B 24; 16 |
| $\varphi_k$ | minimum remaining shelf life at production | RM3/RM4 36; RM1/RM2 0 |
| $\beta$ | fill-rate target of the product, the same for every channel (H04) | 0.95 (Items!TargetFillRate) |
| $R$ | review period: weeks between model runs (A03) | 1 |
| $b_k$ | BOM quantity per FG | 1, 1, 0.5 kg, 0.2 kg |
| $MOQ^P, m^P, U^P$ | production MOQ, batch, maximum per order | 6000, 100, 50000 |
| $MOQ_k, m_k, \bar U_k$ | RM MOQ, multiple, effective maximum | RM1 720/720/19440 · RM2 1440/720/39600 · RM3 200/10/1000 · RM4 1000/100/10000 |
| $L^P_{n\omega}, L^k_{n\omega}$ | lead time of order $n$ (drawn from LeadTimeDist) | fixed: P 6; RM 4/4/8/12 |
| $D_{ct\omega}$ | demand, drawn from the 10 quantiles and rounded to whole units, **independently per channel and week** (G02) | |
| $\mu_t$ | expected total demand of week $t$ = Σ over channels of the mean of the 10 quantiles (K07) | |
| $h^{FG}, h_k$; $w^{FG}, w_k$ | holding / scrap cost per unit | Costs sheet |
| $f_k, v_k$ | RM fixed cost per order, variable cost per unit | Costs sheet |
| $F^P$; $p^P(q), p^T(q)$ | production fixed cost; tier price production / transport | CostTiers |
| $X^{FG}_{0,a}, X^k_{0,a}$, open orders | initial state | Init_Inventory, Open_Orders |

## 2. Decision variables (Seeker)

For stock point $j\in\mathcal J=\{FG\}\cup\mathcal K$ and active week $t\in\mathcal T^{act}_j$:

$$\sigma_{jt}\in\{0,\dots,\bar\sigma_j\},\quad \delta_{jt}\in\{0,\dots,\bar\delta_j\}\ \text{(integer)},\qquad s_{jt}=m_j\,\sigma_{jt},\quad S_{jt}=s_{jt}+m_j\,\delta_{jt}$$

- $S\ge s$ holds by construction (C05). The granularity $m_j$ is the batch or order multiple (K01).
- **Active weeks (K04):** $\mathcal T^{act}_{FG}=\{1..H-\underline L^P\}=\{1..30\}$ and $\mathcal T^{act}_k=\{1..H-\underline L^P-\underline L^k\}$,
  i.e. RM1/RM2 1..26, RM3 1..22, RM4 1..18. Outside these weeks $s=S=0$ ("never order"), because such orders could not arrive
  within the horizon.
- **Bounds (K05):** lower 0. Upper bounds come from the forecast over a cover window (EXTRA_COVER_WEEKS per stock point): the
  channels' weekly Q95 values added up. That is a deliberately generous ceiling, not the Q95 of total demand. The bounds are
  widened to contain the start policy (K12).
- **244 integer variables.** One $(s,S)$ per stock point and week, for every R.

## 3. State dynamics (per scenario ω and week t; definitional, in the event order of A04)

**Runs and the planned position (A03).** The model is run in weeks $1, 1+R, 1+2R, \dots$. The run that plans week $t$ is
$r(t)=1+R\lfloor (t-1)/R\rfloor$. Reviews happen at the **start** of the week, before demand. The decisions of weeks $1..R$
therefore depend only on the state at the run and on the forecast; they are exact and the same in every scenario.

The (s,S) rules act on a position $\widetilde{IP}$: the actual position (below) in a run week, the **planned position** in the
weeks between runs:

$$\widetilde{IP}^{FG}_t=\begin{cases}IP^{FG}_t & t=r(t)\\ \max\big(0,\ \widetilde{IP}^{FG}_{t-1}+\hat q^{\,plan}_{t-1}-\mu_{t-1}\big) & \text{otherwise}\end{cases}\qquad
\widetilde{IP}^{k}_t=\begin{cases}IP^{k}_t & t=r(t)\\ \max\big(0,\ \widetilde{IP}^{k}_{t-1}+q^k_{t-1}-b_k\,\mu_{t-1}\big) & \text{otherwise}\end{cases}$$

- The planned order is $\hat q^{\,plan}_t=\mathbb 1[\widetilde{IP}^{FG}_t<s_{FG,t}]\cdot\hat q_t$, before the raw-material check of E2.
- The planned position never falls below 0 (lost sales). So $s=0$ still means "never order" (C06).
- It is age-blind like the actual position (K22).
- With $R=1$ every week is a run week and $\widetilde{IP}=IP$: the weekly model.
- This is the "reference inventory position" (latest observed position + orders placed since) of inventory problems with
  periodic information delays (Bensoussan, Çakanyıldırım & Sethi 2006, 2007). Subtracting $\mu$ only shifts the (s,S) levels,
  which are free per week.

**E1 Receipts.** Every order $n$ (open or new) with arrival $A_n=t$ enters as a lot with full shelf life (F01):
$$X^{FG}_{t,SL^{FG}} \mathrel{+}= \textstyle\sum_{n:A^P_n=t} q^P_n,\qquad X^k_{t,SL^k} \mathrel{+}= \textstyle\sum_{n:A^k_n=t} q^k_n$$

**E2 FG review and production** (C01, C02, C06, D02–D04). The actual position counts the stock sellable this week plus the
production pipeline. It is age-blind: lots that will expire before they are sold still count (K22).
$$IP^{FG}_t=\sum_{a\ge\rho^{min}}X^{FG}_{t,a}+\sum_{n:\,\text{placed}<t,\ A^P_n>t}q^P_n$$
$$\hat q_t=\min\!\Big(U^P,\ \max\big(MOQ^P,\ m^P\lceil (S_{FG,t}-\widetilde{IP}^{FG}_t)/m^P\rceil\big)\Big)$$
$$\text{avail}_k=\sum_{a\ge\varphi_k}X^k_{t,a},\qquad \check q=\min\!\Big(\hat q_t,\ m^P\Big\lfloor \min_k \tfrac{\text{avail}_k}{b_k}\,/\,m^P\Big\rfloor\Big)$$
$$q^P_t=\mathbb 1[\widetilde{IP}^{FG}_t<s_{FG,t}]\cdot\mathbb 1[\check q\ge MOQ^P]\cdot\check q,\qquad A^P_t=\max\big(t+L^P_{t\omega},\ A^P_{prev}\big)\ \ \text{(no overtaking, E04)}$$
Then $b_k q^P_t$ of each raw material leaves the RMW in the same week, FEFO among lots with $a\ge\varphi_k$ (B02). Several orders
may be in the pipeline at once (D04). A production order planned at a run can come out smaller if its raw material is not on
hand in its week.

**E3 RM review on the echelon** (C03, C04, D01), after E2:
$$IP^k_t=\sum_{a\ge\varphi_k+1}X^{k,E2}_{t,a}+\sum_{n:\,A^k_n>t}q^k_n+b_k\big(IP^{FG}_t+q^P_t\big)$$
$$q^k_t=\mathbb 1[\widetilde{IP}^k_t<s_{kt}]\cdot\min\!\Big(\bar U_k,\ \max\big(MOQ_k,\ m_k\lceil(S_{kt}-\widetilde{IP}^k_t)/m_k\rceil\big)\Big),\qquad A^k_t=\max\big(t+L^k_{t\omega},A^k_{prev}\big)$$
For RM1/RM2 (non-perishable) all stock counts and $\varphi_k=0$.

**E4 Demand and allocation** (H01–H03). Channels are served in ShortagePriority order. Channel $c$ takes FEFO (ascending $a$)
from lots with $a\ge\rho_c$:
$$u_{c,t,a}=\min\Big(X^{(c^-)}_{t,a},\ D_{ct}-\textstyle\sum_{\rho_c\le a'<a}u_{c,t,a'}\Big),\qquad y_{ct}=\textstyle\sum_{a\ge\rho_c}u_{c,t,a}$$
Unserved demand is lost (G04). $FR_{ct}=y_{ct}/D_{ct}$, or $1$ if $D_{ct}=0$ (H04).

**E5 Holding** on the end-of-week stock on hand:
$$\text{Hold}_t=h^{FG}\sum_a X^{FG,end}_{t,a}+\sum_k h_k\sum_a X^{k,end}_{t,a}$$

**E6 Ageing and immediate waste** (F05, C02, C04):
$$W^{FG}_t=\sum_{a\le\rho^{min}}X^{FG,end}_{t,a},\qquad W^k_t=\sum_{a\le\varphi_k}X^{k,end}_{t,a}\ (k\in\mathcal K^p)$$
$$X_{t+1,a}=X^{end}_{t,a+1}\ \text{for usable }a$$

## 4. Objective: expected total cost over the horizon (I01–I07)

$$\min\ Z=\frac1N\sum_{\omega}\sum_{t=1}^{H}\Big[\underbrace{\sum_k\big(f_k\,\mathbb 1[q^k_t>0]+v_k\,q^k_t\big)}_{\text{RM ordering}}+\underbrace{F^P\,\mathbb 1[q^P_t>0]+p^P(q^P_t)\,q^P_t}_{\text{production}}+\underbrace{p^T(q^P_t)\,q^P_t}_{\text{transport KAJA→DC}}+\underbrace{\text{Hold}_t}_{\text{holding}}+\underbrace{w^{FG}W^{FG}_t+\sum_{k\in\mathcal K^p}w_kW^k_t}_{\text{waste}}\Big]$$

- Price tiers per CostTiers (I03, I04).
- Order costs count in the week the order is placed; open orders at the start are sunk.
- No shortage penalty (I01).
- **No value for stock left after week H (A05).** The cheapest plan therefore runs stock down near the end. Only weeks
  $1..R$ are committed and the model is re-run, so this effect must not reach them. Horizon rule (A02, warning X10):
  $H\ge \max_k \bar L^k+\bar L^P+R+8$.

## 5. Constraints

**C1 Fill-rate rule E** (H04–H07), for every channel $c$ and every constrained week $t\in\{t_0..H\}$ with
$t_0=1+\underline L^P=7$ (H06):
$$\frac{1}{N}\sum_\omega FR_{ct\omega}\ \ge\ \beta$$
- On the template that is 3 × 30 = **90 rules**.
- The expected fill rate holds how often and how badly a week is short in one number. Example: 94 futures fully served, 3 at
  80%, 3 at 0% → 96.4%.
- How deep the bad futures go is reported, not constrained: the average fill rate of the worst 5% of futures (L04), computed
  exactly with ties: $\big(E[FR\cdot\mathbb 1(FR<q)]+q\,(0.05-P(FR<q))\big)/0.05$, with $q$ the 5% quantile.

**C2 Domains** as in §2. Everything else holds by construction of §3: stock balance, FEFO, shelf life, MOQ/batch/caps, BOM,
raw-material availability, no overtaking, one production start per week, lost sales.

## 6. Mapping to Seeker (stochastic environment, K02, K06, K09)

```text
env   = Env(license, stochastic=True); env.set_stochastic_parameters(N, speed=0)
vars  = [env.ordinal(0, ub_i, start_i) for every σ, δ]          # 244, start = start policy after the start check (§7)
D     = env.user_distribution(QuantileDistribution(c, t))        # demand per channel × week, independent (G02)
L     = env.categorical_distribution(probs, values) per order    # only if lead times are uncertain (E03)
§3    : E1–E6 written as Terms per week: lots per remaining shelf life, FEFO via
        take = min(lot, max_0(need − older stock)), (s,S) trigger as 0/1 Terms on the actual or planned position,
        ceil/floor rounding, tiers as nested if_, arrival = max(t + L, previous arrival)
cost  = Σ_t (ordering + production + transport + holding + waste)             # stochastic Term
obj   = env.aggregate_mean(cost)                                               # expected cost (§4)
fr    = env.aggregate_mean(FR_ct)                                              # expected fill rate per (c, t)
env.enforce_geq(fr, β, "FR_c_wt")  for every constrained (c, t)                # C1, 90 rules
env.minimize(obj, time_limit)
```

- Every state follows from the decisions by these definitional equations; the only enforced constraints are C1.
- Results are read only as single numbers (aggregates), never as per-scenario lists (K14).
- **Out-of-sample check (K03):** the same model is built with fresh seeds and the decisions fixed, and evaluated once.
- **Feasibility check (K17):** evaluates the maximum-supply policy with the same model.

## 7. Start policy and start check (K07, K21)

Seeker starts from a formula policy (no model evaluation). With $\sigma_u=\sqrt{\sum_c((Q95_{cu}-Q05_{cu})/3.29)^2}$:

$$\text{cover}(t,W)=\sum_{u=t}^{t+W-1}\mu_u+z\sqrt{\sum_{u=r(t)}^{t+W-1}\sigma_u^2}\qquad\text{(cut at the horizon)}$$

$$s_{FG,t}=\text{cover}(t,\bar L^P+1+2),\qquad S_{FG,t}=s_{FG,t}+\text{lot},\qquad \text{lot}=\max\big(MOQ^P,\ \min(4\,\bar\mu,\ \text{fresh-lot sales})\big)$$

$$s_{kt}=\max\big(b_k\,\text{cover}(t,\bar L^k+\bar L^P+1+4),\ b_k\,(s_{FG,t}+MOQ^P)\big),\qquad S_{kt}=s_{kt}+\max(MOQ_k,\ b_k\,\text{lot})$$

- **The safety window starts at the run week $r(t)$.** Between runs the planned position only knows the expected demand since
  the run, so the safety stock grows within a cycle.
- **The second term in $s_{kt}$ is the lot guard.** When FG needs a lot, raw material for one MOQ is on hand or on order.
- **Start factor.** $z_0$ follows from the target via the normal loss function $G(z)=\varphi(z)-z(1-\Phi(z))$:
  $$z_0=G^{-1}\big((1-\beta)\,\bar\mu/\sigma_W\big),\qquad \sigma_W=\sqrt{W\cdot\overline{\sigma^2}},\quad W=\bar L^P+1+2$$
  On the template $z_0$ = 0.86 / 1.22 / 1.63 for β = 90 / 95 / 98%.
- **Start check:**
  1. The start is evaluated once.
  2. If it misses rules that the maximum-supply policy meets, $z$ is raised on a fixed 0.5 grid (above $z_0+0.1$) up to 4.
  3. Only if rules are still missed, the repair (K21) raises supply step by step before the missed weeks, and Seeker starts
     from the cheapest step that meets the most rules.
