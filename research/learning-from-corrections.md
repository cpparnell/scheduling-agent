# Prior art: learning from sparse user Corrections

Resolves [#23](https://github.com/cpparnell/scheduling-agent/issues/23) (map [#20](https://github.com/cpparnell/scheduling-agent/issues/20)).

**Question.** With only tens of Corrections a month, each with a one-tap Correction reason or short free text, which techniques actually improve an LLM extraction system? The four candidates are retrieving past Corrections into the prompt, per-contact priors, extracting rules from Corrections, and calibrating the routing threshold between auto-create and Suggestion. For each one: how much data does it need, and how does it fail?

**Setting assumed.** There is one user. Expect roughly 20–60 Corrections a month, so about 250–700 a year. Implicit accepts (a Suggestion tapped, an auto-created Commitment left alone) are far more numerous. Claude is a generative model, and its stated confidence is not calibrated (see §4). Jev returns probabilities its makers describe as calibrated, but only for typed questions over a JSON state. It does not generate text and takes no few-shot examples in the usual sense (see `scheduling_agent/jev_client.py`). The v1.0 map also plans a labeled corpus covering about 3 months of real `chat.db` history.

---

## 1. Retrieving past Corrections into the prompt (few-shot memory)

**What the sources show**

- **kNN selection of examples beats random selection.** Liu et al. picked in-context examples by semantic similarity to the test input. This beat random sampling on every benchmark they tried, with large gains on some tasks (ToTTo, NQ). Encoders fine-tuned for the task retrieved even better examples. [Liu et al. 2021, arXiv:2101.06804](https://arxiv.org/abs/2101.06804)
- **A memory of user feedback, applied only on a close match, fixes repeat errors without retraining.** MemPrompt stores pairs of (input, user's clarifying feedback). On a new input it retrieves feedback from similar past inputs and adds it to the prompt.
  - It applies feedback only when similarity clears a threshold: cosine 0.9 with Sentence Transformers for the lexical tasks.
  - Ethics accuracy rose from 48.3 to 60.0 (ert-cat) and from 34.4 to 45.2 (ert-nl). The lexical-QA average went from 0.37 to 0.98.
  - Gains build over a few hundred interactions. On ert, the no-memory baseline flattens after about 200 steps while MemPrompt keeps improving.
  - Combining it with KATE-style example retrieval added about 10% more.
  - Failure data: 18% of ert-nl errors were retrieval failures, and about half were "distracted understanding", where the retrieved feedback pulled the model off course.
  - The authors ran **no experiments on noisy or wrong feedback**. Their Limitations section assumes users never give adversarial feedback and calls that assumption unrealistic.

  [Madaan et al., EMNLP 2022, arXiv:2201.06009](https://ar5iv.labs.arxiv.org/html/2201.06009)
- **Retrieval and distilled insights do best together.** In ExpeL, retrieving similar past successes by task similarity (top-k, with k=2–6) and using them as few-shot examples beat reasoning-similarity retrieval (59.0 vs 48.5 on ALFWorld) and random selection (42.5). [Zhao et al., AAAI 2024, arXiv:2308.10144](https://arxiv.org/html/2308.10144)
- **Demonstrations teach format and label space more than correctness.** Replacing demonstration labels with random ones "barely hurts" performance across 12 models. Demonstrations mostly convey the label space, the input distribution, and the format. [Min et al., EMNLP 2022, arXiv:2202.12837](https://arxiv.org/abs/2202.12837)
- **The composition and order of examples bias the output.** The model favours answers that appear often in the prompt and answers placed near the end of it. [Zhao et al., ICML 2021, arXiv:2102.09690](https://arxiv.org/abs/2102.09690) Example order alone can swing results from near-SOTA to near-chance, and an order that works for one model may not work for another. [Lu et al., ACL 2022, arXiv:2104.08786](https://arxiv.org/abs/2104.08786)
- **Anthropic's guidance** is to use 3–5 examples that are relevant (they mirror the real case) and diverse (so Claude "doesn't pick up unintended patterns"), wrapped in `<example>` tags. [Claude prompting best practices](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)

**Data needs.** This technique works from the first Correction. Nothing has to be fit, so it is the only one of the four that pays off at tens of Corrections. Its value depends on recurrence: it helps when the same contact, phrasing, or situation comes up again ("lol we should" with Sam is never a plan). It does nothing for a novel error.

**Failure modes**

- **Overfitting to one Correction.** One retrieved example of "not mine" can push the model to reject a near-miss that really is a Commitment. Min et al. predict this: the model copies the surface pattern even when the label does not apply. Mitigations: a high similarity threshold as in MemPrompt, retrieving the *original input* together with the corrected output rather than a bare instruction, and including retrieved accepted (correct) cases alongside the Corrections.
- **Label imbalance.** If retrieved examples are mostly Corrections of false positives, the majority-label and recency biases from Zhao et al. push the model toward "no plan". Balance positive and negative examples, and put the most similar example last only deliberately.
- **Stale memory and drift.** An old Correction can contradict newer behaviour, for example a contact whose texting style changed. Weight examples by recency and allow a Correction to be superseded.
- **Cost and latency.** Dynamic examples change from call to call, so they must come *after* the static system prompt. Otherwise they break prompt caching.
- **Jev.** Jev answers questions about a JSON state. Past Corrections could be added to the state as fields, but nothing in the repo or SDK docstring says whether Jev conditions on them in a useful way. This is **unverified**: treat retrieval as a Claude-path technique unless an eval shows otherwise.

## 2. Per-contact / per-thread priors

**What the sources show.** The textbook answer for many small groups (contacts) with few observations each is **partial pooling (empirical-Bayes shrinkage)**. Each contact's rate is estimated as a weighted mix of its own data and the population rate, and the weight on its own data grows with its sample size. Efron & Morris showed with baseball batting averages that the shrunk estimates beat each player's raw average at predicting the rest of the season. [Efron & Morris 1975, "Data Analysis Using Stein's Estimator and Its Generalizations", JASA 70(350):311–319](https://doi.org/10.1080/01621459.1975.10479864) The concrete form for accept/correct counts is a Beta-Binomial: `p_contact = (k + a) / (n + a + b)`, where `a/(a+b)` is the global rate and `a+b` is a pseudo-count, typically 5–20.

**Data needs.** Even if the population rate has hundreds of observations behind it, most contacts will have 0–3 Corrections in a year. With a pseudo-count of 10, one Correction moves a contact's estimate by at most about 9 percentage points (1/11). With no shrinkage, the same single Correction would move it to 0% or 100%.

**Failure modes**

- **No shrinkage.** One Correction then defines the contact, and the classic case is "Sam's plans are never real." Always shrink.
- **Putting the prior in prose.** "This contact often cancels" in the prompt invites the LLM to over-weight it, the same anchoring as in §1. A prior is better used numerically as a **routing adjustment**: it shifts the auto-create/Suggestion threshold for that thread, and the extractor itself is left untouched.
- **Confounding.** A contact's Correction rate mixes several causes: the contact's style, the kinds of plans made with them, and how often the user actually looks. Group chats differ from 1:1 threads. Keying priors by thread, with a fallback to the contact and then to global, is safer than keying by contact alone.
- **Drift.** Relationships change, so decay old counts (see §5).

## 3. Rule extraction from Corrections

**What the sources show**

- **ExpeL** has an LLM compare success/failure pairs and maintain a shared list of natural-language insights through four operations: ADD, EDIT, UPVOTE and DOWNVOTE.
  - Each insight starts with an importance count of 2. UPVOTE and EDIT raise it and DOWNVOTE lowers it. At 0 the insight is deleted. The authors say this design protects against misleading single experiences.
  - In ablations, insights alone beat retrieval alone on HotpotQA (36% vs 31%) but lost on ALFWorld (50% vs 55%). Together they scored highest (59% on ALFWorld).
  - Insights extracted from the hand-written few-shots alone gave no gain, so accumulated real experience is what helps.

  [arXiv:2308.10144](https://arxiv.org/html/2308.10144)
- **Hypotheses-to-Theories (HtT)** has the LLM propose rules on training examples. It keeps only rules that appear, and lead to correct answers, "sufficiently often". This gave a 10–30% absolute gain, and the rules transferred across models. [Zhu et al. 2023, arXiv:2310.07064](https://arxiv.org/abs/2310.07064)

**Data needs.** Both methods depend on *repeated support*. A rule is trusted only after several independent cases agree. At tens of Corrections a month, a rule about a common pattern (for example "'maybe next week' with no day is not a Commitment") gathers support within weeks. A rule about one contact or a rare situation never does, and should stay a retrieved example (§1).

**Failure modes**

- **Over-generalization from one Correction.** This is the main risk. An LLM asked to "write a rule" from one Correction writes a general rule, such as "never calendar tentative plans", which contradicts the product definition: tentative plans *are* Commitments. Mitigations from the sources: require a support count of at least 2–3 independent Corrections before activating a rule, and remove rules that later evidence contradicts.
- **Silent regressions.** A rule that fixes one case can break others. Every candidate rule should be gated on the golden suite plus the labeled corpus, using CLAUDE.md's `--repeat 3` failure-set diff, before it goes live.
- **Rule-list bloat.** ExpeL itself notes that lifelong use needs extra retrieval to keep insights within the context window. Cap the active list.
- **A single user can confirm rules.** The research systems vote automatically. Here, one tap ("Always treat X this way?") is cheap and replaces most of the support threshold. This is a judgement, not a sourced result.

## 4. Calibrating the routing threshold from accept/reject signals

**What the sources show**

- **Modern networks are miscalibrated, but a one-parameter fix often works.** Temperature scaling, a single-parameter variant of Platt scaling, recalibrates well on most datasets. Because it fits only one or two parameters, it needs far less data than retraining. [Guo et al., ICML 2017, arXiv:1706.04599](https://arxiv.org/abs/1706.04599)
- **LLM verbalized confidence is overconfident.** When asked for a confidence number, LLMs "tend to be overconfident". Consistency sampling and better aggregation help, but no method wins consistently. [Xiong et al., ICLR 2024, arXiv:2306.13063](https://arxiv.org/abs/2306.13063) Claude's `confidence` field should therefore not route on its raw value.
- **Choosing a threshold with a guarantee.** Selective classification picks a confidence cutoff so that the error rate on accepted items stays below a target with high probability. [Geifman & El-Yaniv 2017, arXiv:1705.08500](https://arxiv.org/abs/1705.08500)

  Conformal prediction and risk control give finite-sample guarantees under exchangeability:
  - The threshold is the ⌈(n+1)(1−α)⌉/n quantile of calibration scores.
  - Conformal risk control picks a threshold for any monotone loss, such as the false-auto-create rate. Learn-then-Test handles non-monotone losses.
  - Data needs, from Table 1 (α=0.1, δ=0.1): to land within ±10 points of the target, n ≈ 22 calibration examples suffice; ±5 points needs about 102 and ±1 point about 2,491. Their general guideline is n ≈ 1,000.

  [Angelopoulos & Bates, arXiv:2107.07511](https://ar5iv.labs.arxiv.org/html/2107.07511)
- **The binomial arithmetic (standard).** Estimating auto-create precision from live outcomes:
  - 24 correct out of 30 gives a 95% Wilson interval of about [0.63, 0.91].
  - 80 out of 100 narrows it only to about ±8 points.

  [Brown, Cai & DasGupta 2001, "Interval Estimation for a Binomial Proportion", Statistical Science 16(2)](https://doi.org/10.1214/ss/1009213286) A month of live signals cannot tell a precision of 0.85 from one of 0.92.
- **Feedback loops and selection bias.** A model whose outputs shape its own future training data creates hidden feedback loops. [Sculley et al., NeurIPS 2015, "Hidden Technical Debt in Machine Learning Systems"](https://papers.nips.cc/paper_files/paper/2015/hash/86df7dcfd896fcaf2674f757a2463eba-Abstract.html) Bottou et al. show that learning what *would* happen under a different policy requires randomized exploration and logged propensities. [Bottou et al., JMLR 14, 2013](https://jmlr.org/papers/v14/bottou13a.html)

  Here that means:
  - Candidates below the Suggestion floor are never shown, so missed Commitments produce no signal.
  - Auto-created events produce a signal only when the user notices and corrects them, so silence is not proof of correctness.
  - Suggestions produce explicit accept/reject, but only within the band where they appear.

**Data needs**

- Platt or temperature scaling needs roughly 100+ labeled outcomes to fit stably. This is a judgement extrapolated from Guo et al.'s low parameter count, not a stated figure.
- A precision guarantee within ±5 points needs about 100 calibration points, per Angelopoulos & Bates.
- Live Corrections reach those counts in months. The planned **3-month labeled corpus reaches them on day one.**

**Failure modes**

- **Calibrating on biased live data.** Fitting the threshold only on Suggestions that were shown censors the band below the floor and the unnoticed auto-creates.
- **"No undo means correct."** This overstates precision, because the user may not have looked at the event.
- **Moving the threshold after each Correction.** This chases noise, given the interval widths above.
- **Trusting Jev's stated calibration without checking.** Jev's probabilities are calibrated on its makers' data, not this user's messages. Verify with a reliability check on the labeled corpus before relying on them. This is unverified here.
- **Drift.** Exchangeability fails over time (§5).

## 5. Drift (cross-cutting)

Concept drift, a change over time in the relationship between inputs and the target, is standard in online learning. Adaptation strategies include windowing, forgetting by decay, and drift detection followed by a reset. [Gama et al., "A Survey on Concept Drift Adaptation", ACM Computing Surveys 46(4), 2014](https://doi.org/10.1145/2523813)

For thresholds, Angelopoulos & Bates (§4.6) give weighted conformal with recency weights, such as a rolling window or 0.99^(n−i+1) decay. They warn that variance grows as the effective sample size `n_eff` shrinks: heavy decay on sparse data makes the threshold unstable. With tens of signals a month, use mild decay over months, not days.

## 6. Fine-tuning

Not evaluated in depth. Hundreds of examples a year is too few to fine-tune safely. The four techniques above need no training. ExpeL cites cost and loss of generalization as reasons to avoid fine-tuning in this regime.

---

## Recommendation

1. **Calibrate offline, monitor online.** Set the auto-create/Suggestion threshold with conformal risk control (or simply the empirical precision curve) on the **labeled 3-month corpus**, which has hundreds of points. Do not set it from live Corrections, which are tens of points and selection-biased. Use live accept/reject only to *detect* drift, by watching a rolling precision with a Wilson interval, and re-calibrate when the interval clearly excludes the target.
2. **Route on Jev's probability only after verifying it.** Run a reliability check on the corpus, applying temperature or Platt scaling if needed. Do not route on Claude's verbalized `confidence`.
3. **Build Correction memory (kNN retrieval) first.** It is the only technique that helps from the first Correction.
   - Use a high similarity threshold.
   - Retrieve the *input plus the corrected output plus the reason*, balanced with similar accepted cases. Cap at 3–5 examples.
   - Place them after the cached static prompt.
   - Use it on the Claude path. Treat Jev conditioning on retrieved Corrections as unverified until an eval shows it works.
4. **Per-thread priors as numeric routing nudges, not prompt text.** Use Beta-Binomial shrinkage with a pseudo-count of about 10, fall back from thread to contact to global, and decay over months.
5. **Rules last, and gated.** Propose a rule only after at least 2–3 independent Corrections share a reason. Confirm it with one user tap. Activate it only if the `--repeat 3` failure-set diff on golden plus corpus shows no regression. Delete rules that are contradicted.
6. **Plan for exploration.** Surface a small random fraction of just-below-floor candidates as Suggestions, with the routing probability logged, so the system can measure what it misses. Without this, false negatives are invisible to every technique above.
7. **Every Correction with an LLM-output cause also becomes a golden eval case,** per CLAUDE.md. Then each technique's effect is measured by failure-set diffs rather than by trusting the live loop.
