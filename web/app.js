"use strict";

const DATA_FILES = {
  metadata: "data/metadata.json",
  taxonomy: "data/taxonomy.json",
  motions: "data/motions.json",
  votes: "data/votes.json",
  reports: "data/report_cards.json",
  featured: "data/featured_motions.json",
};

const VOTE_ORDER = [
  "In Favour",
  "In Opposition",
  "Abstain",
  "Absent",
  "Declared Conflict",
  "No Vote",
  "Ineligible",
];
const POSITION_VOTES = new Set(["In Favour", "In Opposition"]);
const BALLOT_KEY = "vancouver-votes-ballot-v1";
const ALIGNMENT_UNLOCK_COUNT = 10;
const PAGE_SIZE = 40;

const state = {
  data: null,
  maps: null,
  ballot: loadBallot(),
  selectedMotionId: null,
  alignmentIndex: 0,
  alignmentVisited: false,
  alignmentRemainderStarted: false,
  alignmentResultsRevealed: false,
  includeFormerMembers: false,
  alignmentQuestions: null,
  cardCategory: "all",
  cardDecision: "divided",
  motionLimit: PAGE_SIZE,
  filters: {
    search: "",
    category: "all",
    stage: "all",
    decision: "all",
    member: "all",
    vote: "all",
    meeting: "all",
    year: "all",
    sort: "newest",
  },
};

const elements = {};

document.addEventListener("DOMContentLoaded", boot);

async function boot() {
  cacheElements();
  bindEvents();
  try {
    const [metadata, taxonomy, motions, votes, reports, featured] = await Promise.all(
      Object.values(DATA_FILES).map(fetchJson),
    );
    state.data = { metadata, taxonomy, motions, votes, reports, featured };
    state.maps = buildMaps(state.data);
    state.alignmentQuestions = selectAlignmentQuestions(featured);
    populateControls();
    renderChrome();
    elements.loading.hidden = true;
    elements.app.hidden = false;
    route();
  } catch (error) {
    console.error(error);
    elements.loading.hidden = true;
    elements.error.hidden = false;
    elements.error.innerHTML = `<div><strong>Voting records could not be loaded.</strong><br>${escapeHtml(error.message)}<br><small>Start the app with <code>python3 scripts/serve.py</code> instead of opening the HTML file directly.</small></div>`;
  }
}

function cacheElements() {
  [
    "loading", "error", "app", "status-banner", "dataset-summary", "member-search",
    "card-category", "card-decision", "card-result-count", "member-cards", "member-profile",
    "member-categories", "motion-search", "motion-category", "motion-stage", "motion-decision", "motion-member",
    "motion-vote", "motion-meeting", "motion-year", "motion-sort", "motion-result-count",
    "motion-list", "load-more", "clear-filters", "match-note", "match-results",
    "ballot-motions", "clear-ballot", "ballot-count", "footer-source", "motion-dialog",
    "dialog-content", "alignment-progress-label", "alignment-progress-context", "alignment-progress-bar", "report-method-note",
    "alignment-question", "alignment-results-kicker", "alignment-results-note", "include-former-members", "alignment-results", "clear-alignment",
    "ballot-include-former-members",
  ].forEach((id) => {
    elements[toCamel(id)] = document.getElementById(id);
  });
  elements.views = [...document.querySelectorAll(".view")];
  elements.navLinks = [...document.querySelectorAll("[data-nav]")];
  elements.dialogClose = document.querySelector(".dialog-close");
}

function bindEvents() {
  window.addEventListener("hashchange", route);
  document.addEventListener("click", handleActionClick);
  elements.memberSearch.addEventListener("input", renderCards);
  elements.cardCategory.addEventListener("change", () => {
    state.cardCategory = elements.cardCategory.value;
    renderCards();
  });
  elements.cardDecision.addEventListener("change", () => {
    state.cardDecision = elements.cardDecision.value;
    renderCards();
  });
  [
    ["motionSearch", "search", "input"],
    ["motionCategory", "category", "change"],
    ["motionStage", "stage", "change"],
    ["motionDecision", "decision", "change"],
    ["motionMember", "member", "change"],
    ["motionVote", "vote", "change"],
    ["motionMeeting", "meeting", "change"],
    ["motionYear", "year", "change"],
    ["motionSort", "sort", "change"],
  ].forEach(([elementName, key, eventName]) => {
    elements[elementName].addEventListener(eventName, () => {
      state.filters[key] = elements[elementName].value.trim();
      state.motionLimit = PAGE_SIZE;
      renderMotions();
    });
  });
  elements.loadMore.addEventListener("click", () => {
    state.motionLimit += PAGE_SIZE;
    renderMotions();
  });
  elements.clearFilters.addEventListener("click", clearMotionFilters);
  elements.clearAlignment.addEventListener("click", () => {
    const questions = state.alignmentQuestions;
    const featuredIds = new Set(questions.map((item) => item.motion_id));
    const hasAnswers = Object.keys(state.ballot).some((motionId) => featuredIds.has(motionId));
    if (!hasAnswers) return;
    if (window.confirm(`Clear your answers to these ${questions.length} curated questions? Other saved ballot choices will remain.`)) {
      featuredIds.forEach((motionId) => delete state.ballot[motionId]);
      state.alignmentIndex = 0;
      state.alignmentRemainderStarted = false;
      state.alignmentResultsRevealed = false;
      saveBallot();
      renderChrome();
      renderAlignment();
    }
  });
  elements.includeFormerMembers.addEventListener("change", () => {
    state.includeFormerMembers = elements.includeFormerMembers.checked;
    renderAlignment();
  });
  elements.ballotIncludeFormerMembers.addEventListener("change", () => {
    state.includeFormerMembers = elements.ballotIncludeFormerMembers.checked;
    renderBallot();
  });
  elements.clearBallot.addEventListener("click", () => {
    if (!Object.keys(state.ballot).length) return;
    if (window.confirm("Clear all of your saved motion choices from this browser?")) {
      state.ballot = {};
      saveBallot();
      renderChrome();
      renderBallot();
    }
  });
  elements.dialogClose.addEventListener("click", () => elements.motionDialog.close());
  elements.motionDialog.addEventListener("click", (event) => {
    if (event.target === elements.motionDialog) elements.motionDialog.close();
  });
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json();
}

function buildMaps(data) {
  const categories = new Map(data.taxonomy.categories.map((item) => [item.id, item]));
  const directions = new Map(data.taxonomy.policy_directions.map((item) => [item.id, item]));
  const motions = new Map(data.motions.map((item) => [item.motion_id, item]));
  const reports = new Map(data.reports.map((item) => [item.member_id, item]));
  const members = new Map(data.reports.map((item) => [item.member_id, item]));
  const votesByMotion = new Map();
  const votesByMember = new Map();
  const memberMotionVote = new Map();
  data.votes.forEach((vote) => {
    if (!votesByMotion.has(vote.motion_id)) votesByMotion.set(vote.motion_id, []);
    votesByMotion.get(vote.motion_id).push(vote);
    if (!votesByMember.has(vote.member_id)) votesByMember.set(vote.member_id, []);
    votesByMember.get(vote.member_id).push(vote);
    memberMotionVote.set(`${vote.member_id}:${vote.motion_id}`, vote.vote);
  });
  return { categories, directions, motions, reports, members, votesByMotion, votesByMember, memberMotionVote };
}

function populateControls() {
  const presentCategories = new Set(state.data.motions.map((motion) => motion.primary_category));
  const categoryOptions = state.data.taxonomy.categories
    .filter((category) => presentCategories.has(category.id))
    .map((category) => `<option value="${escapeHtml(category.id)}">${escapeHtml(category.label)}</option>`)
    .join("");
  elements.cardCategory.innerHTML = `<option value="all">All headline motions</option>${categoryOptions}`;
  elements.cardDecision.value = state.cardDecision;
  elements.motionCategory.innerHTML = `<option value="all">All categories</option>${categoryOptions}`;

  const members = [...state.data.reports].sort((a, b) => a.name.localeCompare(b.name));
  elements.motionMember.innerHTML = `<option value="all">All members</option>${members.map((member) => `<option value="${escapeHtml(member.member_id)}">${escapeHtml(member.name)}</option>`).join("")}`;
  elements.motionVote.innerHTML = `<option value="all">Any vote</option>${VOTE_ORDER.map((vote) => `<option value="${escapeHtml(vote)}">${escapeHtml(vote)}</option>`).join("")}`;

  const meetings = [...new Set(state.data.motions.map((motion) => motion.meeting_type))].sort();
  elements.motionMeeting.innerHTML = `<option value="all">All meeting types</option>${meetings.map((meeting) => `<option value="${escapeHtml(meeting)}">${escapeHtml(meeting)}</option>`).join("")}`;
  const years = [...new Set(state.data.motions.map((motion) => motion.vote_date.slice(0, 4)))].sort().reverse();
  elements.motionYear.innerHTML = `<option value="all">All years</option>${years.map((year) => `<option value="${year}">${year}</option>`).join("")}`;
}

function renderChrome() {
  const { metadata } = state.data;
  const selection = metadata.selection;
  elements.datasetSummary.innerHTML = [
    [formatNumber(selection.motions), "motions"],
    [formatNumber(selection.vote_rows), "member votes"],
    [formatNumber(selection.members), "Council members"],
    [`${selection.actual_date_min.slice(0, 4)}–${selection.actual_date_max.slice(0, 4)}`, "record window"],
  ].map(([value, label]) => `<span class="summary-stat"><strong>${value}</strong><span>${label}</span></span>`).join("");

  const classification = metadata.classification;
  const enrichment = metadata.enrichment || {};
  const notices = [];
  if ((enrichment.matched_motions || 0) < (enrichment.total_motions || selection.motions)) {
    notices.push(`<strong>Minutes enrichment:</strong> ${formatNumber(enrichment.matched_motions || 0)} of ${formatNumber(enrichment.total_motions || selection.motions)} motions matched to official minutes.`);
  }
  if (classification.classified_motions < classification.total_motions) {
    notices.push(`<strong>Classification pending:</strong> ${formatNumber(classification.classified_motions)} of ${formatNumber(classification.total_motions)} motions labelled.`);
  }
  if (notices.length) {
    elements.statusBanner.hidden = false;
    elements.statusBanner.innerHTML = notices.join(" &nbsp;·&nbsp; ");
  } else {
    elements.statusBanner.hidden = true;
  }
  const ballotCount = Object.keys(state.ballot).length;
  elements.ballotCount.textContent = String(ballotCount);
  const stages = metadata.motion_stages || {};
  elements.reportMethodNote.textContent = stages.direct_amendments
    ? `Report cards default to divided decisions. Headline totals also omit ${formatNumber(stages.direct_amendments)} motions explicitly identified as direct amendments; both filters can be changed.`
    : "Headline totals currently include every motion.";
  elements.footerSource.innerHTML = `Source file covers ${formatDate(metadata.source.date_min)} to ${formatDate(metadata.source.date_max)}.<br><a href="${escapeHtml(metadata.source.dataset_url)}" target="_blank" rel="noreferrer">City of Vancouver dataset ↗</a>`;
}

function route() {
  if (!state.data) return;
  const hash = window.location.hash || "#alignment";
  let view = "alignment";
  let memberId = null;
  if (hash.startsWith("#member=")) {
    memberId = decodeURIComponent(hash.slice(8));
    view = state.maps.reports.has(memberId) ? "member" : "cards";
  } else if (hash === "#motions") {
    view = "motions";
  } else if (hash === "#alignment") {
    view = "alignment";
  } else if (hash === "#ballot") {
    view = "ballot";
  }
  elements.views.forEach((element) => { element.hidden = element.id !== `${view}-view`; });
  elements.navLinks.forEach((link) => link.classList.toggle("active", link.dataset.nav === (view === "member" ? "cards" : view)));
  if (view === "cards") renderCards();
  if (view === "member") renderMember(memberId);
  if (view === "motions") renderMotions();
  if (view === "alignment") renderAlignment();
  if (view === "ballot") renderBallot();
  window.scrollTo({ top: 0, behavior: "instant" });
}

function renderCards() {
  const query = elements.memberSearch.value.trim().toLocaleLowerCase();
  const reports = state.data.reports
    .filter((report) => !query || `${report.name} ${report.party}`.toLocaleLowerCase().includes(query))
    .sort((a, b) => b.last_vote_date.localeCompare(a.last_vote_date) || a.name.localeCompare(b.name));
  const selectedCategory = state.cardCategory;
  const selectedDecision = state.cardDecision;
  const category = selectedCategory === "all" ? null : state.maps.categories.get(selectedCategory);
  const decisionLabel = selectedDecision === "divided" ? "Divided decisions" : "All decisions";
  elements.cardResultCount.textContent = `${reports.length} ${reports.length === 1 ? "member" : "members"} · ${decisionLabel}${category ? ` · ${category.label}` : ""}`;
  if (!reports.length) {
    elements.memberCards.innerHTML = `<div class="empty-state">No Council members match that search.</div>`;
    return;
  }
  elements.memberCards.innerHTML = reports.map((report) => memberCardHtml(report, selectedCategory)).join("");
}

function memberCardHtml(report, categoryId) {
  const scope = reportScope(report, categoryId);
  const favour = scope.vote_counts["In Favour"] || 0;
  const opposition = scope.vote_counts["In Opposition"] || 0;
  const other = Math.max(0, scope.total - favour - opposition);
  const positionTotal = favour + opposition;
  const match = calculateMatch(report.member_id, categoryId === "all" ? null : categoryId);
  return `<button class="member-card" type="button" data-action="open-member" data-member-id="${escapeHtml(report.member_id)}">
    <span class="card-top">
      <span><span class="eyebrow">${escapeHtml(report.role)}</span><h3>${escapeHtml(shortName(report.name))}</h3>${partyBadge(report)}<span class="member-tenure">Recorded ${formatDate(report.first_vote_date)} – ${formatDate(report.last_vote_date)}</span></span>
      <span class="member-avatar" aria-hidden="true">${escapeHtml(initials(report.name))}</span>
    </span>
    <span class="card-metrics">
      <span class="card-metric"><strong>${formatNumber(scope.total)}</strong><span>${state.cardDecision === "divided" ? "Divided records" : "Headline records"}</span></span>
      <span class="card-metric"><strong>${formatPercent(scope.participation_rate)}</strong><span>Participated</span></span>
      <span class="card-metric"><strong>${match.positions ? formatPercent(match.score) : "—"}</strong><span>Your match</span></span>
    </span>
    <span class="card-split">
      <span class="split-labels"><span>${positionTotal ? `${formatPercent(favour / positionTotal)} favour` : "No positions"}</span><span>${positionTotal ? `${formatPercent(opposition / positionTotal)} opposed` : ""}</span></span>
      ${stackedBar(favour, opposition, other, `${favour} in favour, ${opposition} in opposition, ${other} other records`)}
    </span>
  </button>`;
}

function reportScope(report, categoryId, decisionScope = state.cardDecision) {
  const base = report.decision_scopes?.[decisionScope] || {
    recorded_votes: report.recorded_votes,
    participation_rate: report.participation_rate,
    vote_counts: report.vote_counts,
    categories: report.categories,
  };
  if (categoryId === "all") {
    return {
      total: base.recorded_votes,
      participation_rate: base.participation_rate,
      vote_counts: base.vote_counts,
    };
  }
  const category = base.categories.find((item) => item.category_id === categoryId);
  return category || { total: 0, participation_rate: 0, vote_counts: {} };
}

function renderMember(memberId) {
  const report = state.maps.reports.get(memberId);
  if (!report) return;
  const match = calculateMatch(memberId);
  const decisionScope = report.decision_scopes?.[state.cardDecision] || report;
  const counts = decisionScope.vote_counts;
  const favour = counts["In Favour"] || 0;
  const opposition = counts["In Opposition"] || 0;
  const partySource = safeExternalUrl(report.party_source_url);
  elements.memberProfile.innerHTML = `<header class="profile-header">
    <div><p class="eyebrow">${escapeHtml(report.role)} voting record</p><h1 id="member-title" class="profile-name">${escapeHtml(shortName(report.name))}</h1>${partyBadge(report)}<p class="profile-meta">Record available from ${formatDate(report.first_vote_date)} to ${formatDate(report.last_vote_date)}</p><p class="party-history">${escapeHtml(report.party_history)}${partySource ? ` <a href="${escapeHtml(partySource)}" target="_blank" rel="noreferrer">Source ↗</a>` : ""}</p></div>
    <div class="profile-score"><div><strong>${match.positions ? formatPercent(match.score) : "—"}</strong><span>Your ballot match${match.positions ? `<br>${match.positions} comparable` : "<br>no choices yet"}</span></div></div>
  </header>
  <div class="profile-stats">
    <div class="profile-stat"><strong>${formatNumber(decisionScope.recorded_votes)}</strong><span>${state.cardDecision === "divided" ? "Divided votes" : "Headline votes"}</span></div>
    <div class="profile-stat"><strong>${formatNumber(favour)}</strong><span>In favour</span></div>
    <div class="profile-stat"><strong>${formatNumber(opposition)}</strong><span>In opposition</span></div>
    <div class="profile-stat"><strong>${formatPercent(decisionScope.participation_rate)}</strong><span>Participation</span></div>
  </div>
  <p class="profile-method-note">${state.cardDecision === "divided" ? "Showing decisions with at least one vote on each side; unanimous decisions are available under All decisions. " : "Showing all headline decisions. "}${formatNumber(report.excluded_direct_amendment_votes)} direct-amendment record${report.excluded_direct_amendment_votes === 1 ? "" : "s"} excluded; ${formatNumber(report.all_recorded_votes)} published records remain searchable.</p>`;

  const categoryRows = decisionScope.categories
    .map((item) => ({ ...item, category: state.maps.categories.get(item.category_id) }))
    .filter((item) => item.category)
    .sort((a, b) => b.total - a.total || a.category.label.localeCompare(b.category.label));
  elements.memberCategories.innerHTML = `<div class="breakdown-heading"><div><p class="eyebrow">Categories</p><h2>Votes by category</h2></div><p>Bars include every status on ${state.cardDecision === "divided" ? "divided, " : ""}headline-eligible motions. Direct amendments remain in the explorer but are not counted here.</p></div>
    ${categoryRows.map((item) => categoryRowHtml(report, item)).join("")}`;
}

function categoryRowHtml(report, item) {
  const counts = item.vote_counts;
  const favour = counts["In Favour"] || 0;
  const opposition = counts["In Opposition"] || 0;
  const other = item.total - favour - opposition;
  return `<button type="button" class="category-row" data-action="report-category" data-member-id="${escapeHtml(report.member_id)}" data-category-id="${escapeHtml(item.category_id)}">
    <span><span class="category-name" style="--category-color:${safeColor(item.category.color)}"><span class="category-dot"></span>${escapeHtml(item.category.label)}</span><span class="category-total">${formatNumber(item.total)} records</span></span>
    ${stackedBar(favour, opposition, other, `${favour} in favour, ${opposition} in opposition, ${other} other records`)}
    <span class="category-counts"><span>${favour} favour</span><span>${opposition} opposed</span></span>
  </button>`;
}

function renderMotions() {
  syncFilterControls();
  let motions = state.data.motions.filter(motionMatchesFilters);
  const sort = state.filters.sort;
  motions.sort((a, b) => {
    if (sort === "oldest") return a.vote_start_date_time.localeCompare(b.vote_start_date_time);
    if (sort === "title") return a.agenda_description.localeCompare(b.agenda_description);
    return b.vote_start_date_time.localeCompare(a.vote_start_date_time);
  });
  elements.motionResultCount.textContent = `${formatNumber(motions.length)} ${motions.length === 1 ? "motion" : "motions"}`;
  const visible = motions.slice(0, state.motionLimit);
  elements.motionList.innerHTML = visible.length
    ? visible.map(motionCardHtml).join("")
    : `<div class="empty-state">No motions match these filters.</div>`;
  elements.loadMore.hidden = visible.length >= motions.length;
  if (!elements.loadMore.hidden) elements.loadMore.textContent = `Show more · ${formatNumber(motions.length - visible.length)} remaining`;
}

function motionMatchesFilters(motion) {
  const filters = state.filters;
  if (filters.search) {
    const haystack = [motion.agenda_description, motion.summary, motion.motion_text, ...(motion.keywords || [])].join(" ").toLocaleLowerCase();
    if (!haystack.includes(filters.search.toLocaleLowerCase())) return false;
  }
  if (filters.category !== "all" && motion.primary_category !== filters.category) return false;
  if (filters.stage === "headline" && motion.direct_amendment) return false;
  if (filters.stage === "amendment" && !motion.direct_amendment) return false;
  const divided = motionIsDivided(motion);
  if (filters.decision === "divided" && !divided) return false;
  if (filters.decision === "unanimous" && divided) return false;
  if (filters.meeting !== "all" && motion.meeting_type !== filters.meeting) return false;
  if (filters.year !== "all" && !motion.vote_date.startsWith(filters.year)) return false;
  if (filters.member !== "all") {
    const vote = state.maps.memberMotionVote.get(`${filters.member}:${motion.motion_id}`);
    if (!vote || (filters.vote !== "all" && vote !== filters.vote)) return false;
  } else if (filters.vote !== "all") {
    const includesVote = (state.maps.votesByMotion.get(motion.motion_id) || []).some((vote) => vote.vote === filters.vote);
    if (!includesVote) return false;
  }
  return true;
}

function motionCardHtml(motion) {
  const category = state.maps.categories.get(motion.primary_category);
  const counts = motion.vote_counts;
  const memberVote = state.filters.member !== "all" ? state.maps.memberMotionVote.get(`${state.filters.member}:${motion.motion_id}`) : null;
  const dateParts = formatMotionDate(motion.vote_date);
  return `<button type="button" class="motion-card" data-action="open-motion" data-motion-id="${escapeHtml(motion.motion_id)}">
    <span class="motion-date"><strong>${escapeHtml(dateParts.day)}</strong>${escapeHtml(dateParts.rest)}</span>
    <span><span class="category-badge" style="--category-color:${safeColor(category?.color)}">${escapeHtml(category?.label || "Unclassified")}</span>${motion.direct_amendment ? `<span class="stage-badge">Direct amendment</span>` : ""}<span class="motion-title">${escapeHtml(motion.agenda_description)}</span><span class="motion-meta">${escapeHtml(motion.meeting_type)} · Vote ${escapeHtml(motion.vote_number)} · ${escapeHtml(motion.decision)}${memberVote ? ` · <strong>${escapeHtml(shortVote(memberVote))}</strong>` : ""}</span></span>
    <span class="motion-tally">
      <span class="tally-line"><span>In favour</span><strong>${counts["In Favour"] || 0}</strong></span>
      <span class="tally-line"><span>In opposition</span><strong>${counts["In Opposition"] || 0}</strong></span>
      ${stackedBar(counts["In Favour"] || 0, counts["In Opposition"] || 0, motion.member_vote_count - (counts["In Favour"] || 0) - (counts["In Opposition"] || 0), "Vote split")}
    </span>
  </button>`;
}

function motionIsDivided(motion) {
  return (motion.vote_counts?.["In Favour"] || 0) > 0 && (motion.vote_counts?.["In Opposition"] || 0) > 0;
}

function clearMotionFilters() {
  state.filters = { search: "", category: "all", stage: "all", decision: "all", member: "all", vote: "all", meeting: "all", year: "all", sort: "newest" };
  state.motionLimit = PAGE_SIZE;
  syncFilterControls();
  renderMotions();
}

function syncFilterControls() {
  elements.motionSearch.value = state.filters.search;
  elements.motionCategory.value = state.filters.category;
  elements.motionStage.value = state.filters.stage;
  elements.motionDecision.value = state.filters.decision;
  elements.motionMember.value = state.filters.member;
  elements.motionVote.value = state.filters.vote;
  elements.motionMeeting.value = state.filters.meeting;
  elements.motionYear.value = state.filters.year;
  elements.motionSort.value = state.filters.sort;
}

function openMotion(motionId) {
  const motion = state.maps.motions.get(motionId);
  if (!motion) return;
  state.selectedMotionId = motionId;
  renderMotionDialog(motion);
  if (!elements.motionDialog.open) elements.motionDialog.showModal();
}

function renderMotionDialog(motion) {
  const category = state.maps.categories.get(motion.primary_category);
  const direction = state.maps.directions.get(motion.policy_direction);
  const votes = state.maps.votesByMotion.get(motion.motion_id) || [];
  const grouped = new Map(VOTE_ORDER.map((value) => [value, []]));
  votes.forEach((vote) => {
    if (!grouped.has(vote.vote)) grouped.set(vote.vote, []);
    grouped.get(vote.vote).push(state.maps.members.get(vote.member_id)?.name || vote.member_id);
  });
  const stance = state.ballot[motion.motion_id] || "";
  const classificationHtml = motion.classification_status === "classified"
    ? `<div class="classification-detail"><p><strong>Plain-language summary:</strong> ${escapeHtml(motion.summary)}</p><p><strong>Policy action:</strong> ${escapeHtml(direction?.label || motion.policy_direction)} · <strong>Impact:</strong> ${escapeHtml(motion.public_impact)} · <strong>Confidence:</strong> ${Math.round(motion.confidence * 100)}%</p>${motion.needs_review ? `<p><strong>Review flag:</strong> The label may need the full motion text for confirmation.</p>` : ""}</div>`
    : `<div class="classification-detail"><p><strong>Classification pending.</strong> The original agenda title and vote record are still available below.</p></div>`;
  const sourceUrl = safeExternalUrl(motion.minutes_url);
  const enrichmentHtml = motion.enrichment_status === "matched" && motion.motion_text
    ? `<section class="minutes-detail"><div class="minutes-heading"><h3>What Council voted on</h3><span>${motion.enrichment_match_quality === "exact" ? `Matched by Vote No. ${escapeHtml(motion.vote_number)}` : `Matched by agenda title · minutes show Vote No. ${escapeHtml(motion.minutes_vote_marker || "unknown")}`}</span></div>${motion.moved_by || motion.seconded_by ? `<p class="minutes-people">${motion.moved_by ? `<strong>Moved by:</strong> ${escapeHtml(motion.moved_by)}` : ""}${motion.moved_by && motion.seconded_by ? " · " : ""}${motion.seconded_by ? `<strong>Seconded by:</strong> ${escapeHtml(motion.seconded_by)}` : ""}</p>` : ""}<div class="minutes-text">${escapeHtml(motion.motion_text)}</div><p class="minutes-source">${sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">Open official minutes${motion.minutes_pdf_page ? ` at PDF page ${escapeHtml(motion.minutes_pdf_page)}` : ""} ↗</a>` : ""}${motion.enrichment_match_quality !== "exact" ? `<span>Vote-number discrepancy: verify this inferred match.</span>` : motion.minutes_text_truncated ? `<span>Excerpt was shortened for classification; verify the source.</span>` : ""}</p></section>`
    : `<section class="minutes-detail minutes-missing"><h3>Detailed motion text unavailable</h3><p>This vote has not yet been matched to its official meeting minutes.</p>${sourceUrl ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noreferrer">Try the expected minutes URL ↗</a>` : ""}</section>`;
  elements.dialogContent.innerHTML = `<article class="dialog-body">
    <p class="dialog-kicker">${formatDate(motion.vote_date)} · ${escapeHtml(motion.meeting_type)} · Vote ${escapeHtml(motion.vote_number)}</p>
    <span class="category-badge" style="--category-color:${safeColor(category?.color)}">${escapeHtml(category?.label || "Unclassified")}</span>${motion.direct_amendment ? `<span class="stage-badge">Direct amendment</span>` : ""}
    <h2 id="dialog-title">${escapeHtml(motion.agenda_description)}</h2>
    <p class="dialog-summary">Decision: <strong>${escapeHtml(motion.decision)}</strong></p>
    ${motion.direct_amendment ? `<p class="stage-note">Explicit agenda or minutes language identifies this as an amendment proposed during debate. It remains visible here but is excluded from headline report-card totals.</p>` : ""}
    ${enrichmentHtml}
    ${classificationHtml}
    <div class="stance-box"><p>How would you vote?</p><div class="stance-actions">
      <button type="button" class="stance-button ${stance === "support" ? "selected" : ""}" data-action="set-stance" data-motion-id="${escapeHtml(motion.motion_id)}" data-stance="support">I’d vote in favour</button>
      <button type="button" class="stance-button ${stance === "oppose" ? "selected" : ""}" data-action="set-stance" data-motion-id="${escapeHtml(motion.motion_id)}" data-stance="oppose">I’d vote in opposition</button>
      ${stance ? `<button type="button" class="stance-button" data-action="set-stance" data-motion-id="${escapeHtml(motion.motion_id)}" data-stance="clear">Clear choice</button>` : ""}
    </div></div>
    <section class="vote-section"><h3>Recorded votes</h3><div class="vote-groups">${[...grouped.entries()].filter(([, names]) => names.length).map(([vote, names]) => `<div class="vote-group"><div class="vote-group-heading"><span>${escapeHtml(vote)}</span><span>${names.length}</span></div><ul>${names.sort().map((name) => `<li>${escapeHtml(shortName(name))}</li>`).join("")}</ul></div>`).join("")}</div></section>
    <p class="official-note">This interface reproduces the published open-data record. Consult the corresponding City meeting minutes for the official vote.</p>
  </article>`;
}

function renderAlignment() {
  const questions = state.alignmentQuestions;
  const starterCount = Math.min(state.data.featured.selection.quiz_count || questions.length, questions.length);
  const starterQuestions = questions.slice(0, starterCount);
  const featuredIds = new Set(questions.map((item) => item.motion_id));
  const answered = questions.filter((item) => state.ballot[item.motion_id]);
  const starterAnswered = starterQuestions.filter((item) => state.ballot[item.motion_id]).length;
  if (!state.alignmentVisited) {
    const firstUnanswered = starterQuestions.findIndex((item) => !state.ballot[item.motion_id]);
    const remainderQuestions = questions.slice(starterCount);
    const firstUnansweredRemainder = remainderQuestions.findIndex((item) => !state.ballot[item.motion_id]);
    const hasRemainderAnswers = remainderQuestions.some((item) => state.ballot[item.motion_id]);
    if (firstUnanswered !== -1) {
      state.alignmentIndex = firstUnanswered;
    } else if (hasRemainderAnswers) {
      state.alignmentRemainderStarted = true;
      state.alignmentIndex = firstUnansweredRemainder === -1 ? questions.length : starterCount + firstUnansweredRemainder;
    } else {
      state.alignmentIndex = starterCount;
    }
    state.alignmentVisited = true;
  }
  state.alignmentIndex = Math.max(0, Math.min(state.alignmentIndex, questions.length));
  if (state.alignmentRemainderStarted) {
    elements.alignmentProgressLabel.textContent = `${answered.length} of ${questions.length} answered`;
    elements.alignmentProgressContext.textContent = `${starterCount} starter questions plus ${questions.length - starterCount} additional motions.`;
    elements.alignmentProgressBar.style.width = `${(answered.length / questions.length) * 100}%`;
  } else {
    elements.alignmentProgressLabel.textContent = `${starterAnswered} of ${starterCount} starter questions answered`;
    elements.alignmentProgressContext.textContent = starterAnswered < ALIGNMENT_UNLOCK_COUNT
      ? `Results unlock after ${ALIGNMENT_UNLOCK_COUNT} answers.`
      : "Your live comparison is now available.";
    elements.alignmentProgressBar.style.width = `${(starterAnswered / starterCount) * 100}%`;
  }

  if (state.alignmentIndex === questions.length) {
    if (answered.length >= ALIGNMENT_UNLOCK_COUNT) state.alignmentResultsRevealed = true;
    elements.alignmentQuestion.innerHTML = `<article class="alignment-question-card alignment-complete">
      <p class="eyebrow">All ${questions.length} answered</p>
      <h2>You made all ${questions.length} choices.</h2>
      <p>Your closest Council voting records are shown alongside. A match reflects these ${questions.length} motions—not every issue, value, or qualification.</p>
      <div class="alignment-card-nav"><button type="button" class="secondary-button" data-action="alignment-previous">Review answers</button><a class="primary-button" href="#ballot">See my full ballot</a></div>
    </article>`;
  } else if (state.alignmentIndex === starterCount && !state.alignmentRemainderStarted) {
    if (starterAnswered >= ALIGNMENT_UNLOCK_COUNT) state.alignmentResultsRevealed = true;
    const unlockMessage = starterAnswered >= ALIGNMENT_UNLOCK_COUNT
      ? "Your starter comparison is shown alongside."
      : `Answer ${ALIGNMENT_UNLOCK_COUNT - starterAnswered} more starter question${ALIGNMENT_UNLOCK_COUNT - starterAnswered === 1 ? "" : "s"} to unlock your comparison.`;
    elements.alignmentQuestion.innerHTML = `<article class="alignment-question-card alignment-complete">
      <p class="eyebrow">First ${starterCount} complete</p>
      <h2>${starterAnswered} of ${starterCount} starter choices saved.</h2>
      <p>${unlockMessage} You can review this set or continue through ${questions.length - starterCount} additional motions for a broader comparison.</p>
      <div class="alignment-card-nav"><button type="button" class="secondary-button" data-action="alignment-previous">← Review starter set</button><button type="button" class="primary-button" data-action="alignment-remainder">Continue with ${questions.length - starterCount} more →</button></div>
    </article>`;
  } else {
    const question = questions[state.alignmentIndex];
    const motion = state.maps.motions.get(question.motion_id);
    const category = state.maps.categories.get(motion.primary_category);
    const stance = state.ballot[motion.motion_id] || "";
    const argumentsForAndAgainst = alignmentArguments(question);
    const inStarterSet = state.alignmentIndex < starterCount;
    const phaseNumber = inStarterSet ? state.alignmentIndex + 1 : state.alignmentIndex - starterCount + 1;
    const phaseTotal = inStarterSet ? starterCount : questions.length - starterCount;
    const phaseLabel = inStarterSet ? "Starter question" : "Additional question";
    const nextLabel = state.alignmentIndex === questions.length - 1
      ? "See final results"
      : state.alignmentIndex === starterCount - 1
        ? "Review starter results →"
        : "Skip for now →";
    elements.alignmentQuestion.innerHTML = `<article class="alignment-question-card">
      <div class="alignment-question-meta"><span>${phaseLabel} ${phaseNumber} of ${phaseTotal}</span><span>${formatDate(motion.vote_date)}</span></div>
      <span class="category-badge" style="--category-color:${safeColor(category?.color)}">${escapeHtml(category?.label || "Unclassified")}</span>
      <h2>${escapeHtml(question.prompt)}</h2>
      <div class="alignment-context"><p>${escapeHtml(question.context)}</p></div>
      <div class="alignment-arguments">
        <div class="alignment-argument argument-for"><strong>Supporters’ case</strong><p>${escapeHtml(argumentsForAndAgainst.for)}</p></div>
        <div class="alignment-argument argument-against"><strong>Opponents’ case</strong><p>${escapeHtml(argumentsForAndAgainst.against)}</p></div>
      </div>
      <p class="alignment-argument-note">Concise good-faith arguments, not quotations or attributed motives.</p>
      <p class="alignment-source-title">Council record: ${escapeHtml(motion.agenda_description)}</p>
      <div class="alignment-sources">${question.meeting_agenda_url ? `<a href="${escapeHtml(question.meeting_agenda_url)}" target="_blank" rel="noreferrer">Official agenda & reports ↗</a>` : ""}${question.research_source_url && question.research_source_url !== question.meeting_agenda_url ? `<a href="${escapeHtml(question.research_source_url)}" target="_blank" rel="noreferrer">Additional official source ↗</a>` : ""}</div>
      <div class="alignment-choices" role="group" aria-label="Your vote">
        <button type="button" class="alignment-choice favour ${stance === "support" ? "selected" : ""}" data-action="set-alignment" data-motion-id="${escapeHtml(motion.motion_id)}" data-stance="support"><span>Vote</span>I’d vote in favour</button>
        <button type="button" class="alignment-choice oppose ${stance === "oppose" ? "selected" : ""}" data-action="set-alignment" data-motion-id="${escapeHtml(motion.motion_id)}" data-stance="oppose"><span>Vote</span>I’d vote in opposition</button>
      </div>
      <p class="alignment-neutrality">The actual Council result is hidden while you decide. <button type="button" class="inline-button" data-action="open-motion" data-motion-id="${escapeHtml(motion.motion_id)}">Inspect the full record</button></p>
      <div class="alignment-card-nav">
        <button type="button" class="secondary-button" data-action="alignment-previous" ${state.alignmentIndex === 0 ? "disabled" : ""}>← Previous</button>
        <button type="button" class="secondary-button" data-action="alignment-next">${nextLabel}</button>
      </div>
    </article>`;
  }

  renderAlignmentResults(featuredIds, answered.length);
}

function renderAlignmentResults(featuredIds, answeredCount) {
  elements.includeFormerMembers.checked = state.includeFormerMembers;
  if (answeredCount < ALIGNMENT_UNLOCK_COUNT) {
    elements.alignmentResultsKicker.textContent = `${ALIGNMENT_UNLOCK_COUNT} answers needed`;
    elements.alignmentResultsNote.textContent = `Answer ${ALIGNMENT_UNLOCK_COUNT - answeredCount} more to make a comparison available.`;
    elements.alignmentResults.innerHTML = `<div class="alignment-placeholder"><strong>${answeredCount}</strong><span>of ${ALIGNMENT_UNLOCK_COUNT} answers needed</span></div>`;
    return;
  }
  if (!state.alignmentResultsRevealed) {
    elements.alignmentResultsKicker.textContent = "Ready to view";
    elements.alignmentResultsNote.textContent = "Reveal it now or keep voting. It will be shown automatically when you reach the end of the 20-question starter set.";
    elements.alignmentResults.innerHTML = `<div class="alignment-reveal"><span aria-hidden="true">?</span><button type="button" class="primary-button" data-action="reveal-alignment">Show my comparison</button></div>`;
    return;
  }
  elements.alignmentResultsKicker.textContent = "Your results";
  elements.alignmentResultsNote.textContent = `Based on your ${answeredCount} answered questions. Comparable means the member cast In Favour or In Opposition on that exact motion; abstentions are neutral and excluded.`;
  const minimumComparable = Math.max(3, Math.ceil(answeredCount * 0.35));
  const results = state.data.reports
    .filter((report) => state.includeFormerMembers || report.currently_sitting)
    .map((report) => ({ report, match: calculateMatch(report.member_id, null, featuredIds) }))
    .filter((item) => item.match.positions >= minimumComparable)
    .sort((a, b) => b.match.score - a.match.score || b.match.positions - a.match.positions || a.report.name.localeCompare(b.report.name));
  elements.alignmentResults.innerHTML = results.length
    ? results.map(({ report, match }, index) => `<a class="alignment-result" href="#member=${encodeURIComponent(report.member_id)}">
        <span class="alignment-rank">${index + 1}</span>
        <span><strong>${escapeHtml(shortName(report.name))}</strong>${partyBadge(report)}<small>${match.matches} of ${match.positions} comparable positions matched${match.unavailableByVote.Abstain ? ` · ${match.unavailableByVote.Abstain} abstained` : ""}${match.unavailable - (match.unavailableByVote.Abstain || 0) ? ` · ${match.unavailable - (match.unavailableByVote.Abstain || 0)} unavailable` : ""}</small></span>
        <span class="alignment-score">${formatPercent(match.score)}</span>
      </a>`).join("")
    : `<div class="empty-state">No current member has enough comparable votes yet.</div>`;
}

function renderBallot() {
  const rated = Object.entries(state.ballot)
    .map(([motionId, stance]) => ({ motion: state.maps.motions.get(motionId), stance }))
    .filter((item) => item.motion)
    .sort((a, b) => b.motion.vote_date.localeCompare(a.motion.vote_date));
  const count = rated.length;
  elements.ballotIncludeFormerMembers.checked = state.includeFormerMembers;
  elements.matchNote.textContent = count
    ? `Based on ${count} choice${count === 1 ? "" : "s"}. Comparable means In Favour or In Opposition on the same motion. Abstentions are neutral; absences, conflicts, ineligible and missing records are also excluded.`
    : "Choose motions in the explorer to create a personal comparison.";
  if (!count) {
    elements.matchResults.innerHTML = `<div class="empty-state">No ballot choices yet. Open a motion and mark how you would vote.</div>`;
    elements.ballotMotions.innerHTML = `<p class="result-count">Your selections will appear here.</p>`;
    elements.clearBallot.hidden = true;
    return;
  }
  elements.clearBallot.hidden = false;
  const results = state.data.reports
    .filter((report) => state.includeFormerMembers || report.currently_sitting)
    .map((report) => ({ report, match: calculateMatch(report.member_id) }))
    .sort((a, b) => b.match.score - a.match.score || b.match.positions - a.match.positions || a.report.name.localeCompare(b.report.name));
  elements.matchResults.innerHTML = results.map(({ report, match }) => {
    const misses = match.positions - match.matches;
    const abstained = match.unavailableByVote.Abstain || 0;
    const otherUnavailable = match.unavailable - abstained;
    return `<div class="match-row"><div><a class="match-name" href="#member=${encodeURIComponent(report.member_id)}">${escapeHtml(shortName(report.name))}</a>${partyBadge(report)}<div class="match-context"><span>${match.matches} matching · ${misses} different</span><span>${abstained} abstained · ${otherUnavailable} absent, conflicted, ineligible or without a record</span></div></div><div class="match-bar">${stackedBar(match.matches, misses, match.unavailable, `${match.matches} matching, ${misses} different and ${match.unavailable} without comparable positions`)}</div><div class="match-score"><strong>${match.positions ? formatPercent(match.score) : "—"}</strong><span>${match.positions} comparable</span></div></div>`;
  }).join("");
  elements.ballotMotions.innerHTML = rated.map(({ motion, stance }) => `<div class="ballot-motion"><button type="button" data-action="open-motion" data-motion-id="${escapeHtml(motion.motion_id)}"><strong>${escapeHtml(motion.agenda_description)}</strong><span>${formatDate(motion.vote_date)} · <em class="${stance === "support" ? "stance-support" : "stance-oppose"}">${stance === "support" ? "In favour" : "In opposition"}</em></span></button></div>`).join("");
}

function calculateMatch(memberId, categoryId = null, motionIds = null) {
  let matches = 0;
  let positions = 0;
  let unavailable = 0;
  const unavailableByVote = {};
  Object.entries(state.ballot).forEach(([motionId, stance]) => {
    if (motionIds && !motionIds.has(motionId)) return;
    const motion = state.maps.motions.get(motionId);
    if (!motion || (categoryId && motion.primary_category !== categoryId)) return;
    const vote = state.maps.memberMotionVote.get(`${memberId}:${motionId}`);
    if (!POSITION_VOTES.has(vote)) {
      unavailable += 1;
      const reason = vote || "No record";
      unavailableByVote[reason] = (unavailableByVote[reason] || 0) + 1;
      return;
    }
    positions += 1;
    if ((stance === "support" && vote === "In Favour") || (stance === "oppose" && vote === "In Opposition")) matches += 1;
  });
  return { matches, positions, unavailable, unavailableByVote, score: positions ? matches / positions : 0 };
}

function handleActionClick(event) {
  const target = event.target.closest("[data-action]");
  if (!target || !state.data) return;
  const action = target.dataset.action;
  if (action === "open-member") window.location.hash = `member=${encodeURIComponent(target.dataset.memberId)}`;
  if (action === "open-motion") openMotion(target.dataset.motionId);
  if (action === "alignment-previous") {
    state.alignmentIndex = Math.max(0, state.alignmentIndex - 1);
    renderAlignment();
  }
  if (action === "alignment-next") {
    state.alignmentIndex = Math.min(state.alignmentQuestions.length, state.alignmentIndex + 1);
    renderAlignment();
  }
  if (action === "alignment-remainder") {
    state.alignmentRemainderStarted = true;
    state.alignmentIndex = Math.min(state.data.featured.selection.quiz_count, state.alignmentQuestions.length);
    renderAlignment();
  }
  if (action === "reveal-alignment") {
    state.alignmentResultsRevealed = true;
    renderAlignment();
  }
  if (action === "set-alignment") {
    state.ballot[target.dataset.motionId] = target.dataset.stance;
    saveBallot();
    renderChrome();
    state.alignmentIndex = Math.min(state.alignmentQuestions.length, state.alignmentIndex + 1);
    renderAlignment();
  }
  if (action === "report-category") {
    state.filters.member = target.dataset.memberId;
    state.filters.category = target.dataset.categoryId;
    state.filters.stage = "headline";
    state.filters.decision = state.cardDecision;
    state.filters.vote = "all";
    state.filters.search = "";
    state.motionLimit = PAGE_SIZE;
    window.location.hash = "motions";
    if (window.location.hash === "#motions") renderMotions();
  }
  if (action === "set-stance") {
    const motionId = target.dataset.motionId;
    if (target.dataset.stance === "clear") delete state.ballot[motionId];
    else state.ballot[motionId] = target.dataset.stance;
    saveBallot();
    renderChrome();
    const motion = state.maps.motions.get(motionId);
    if (motion && elements.motionDialog.open) renderMotionDialog(motion);
    if (!document.getElementById("cards-view").hidden) renderCards();
    if (!document.getElementById("alignment-view").hidden) renderAlignment();
  }
}

function loadBallot() {
  try {
    const value = JSON.parse(localStorage.getItem(BALLOT_KEY) || "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, stance]) => stance === "support" || stance === "oppose"));
  } catch {
    return {};
  }
}

function saveBallot() {
  try { localStorage.setItem(BALLOT_KEY, JSON.stringify(state.ballot)); } catch (error) { console.warn("Could not save ballot", error); }
}

function selectAlignmentQuestions(featured) {
  const pool = featured.questions;
  const poolMap = new Map(pool.map((item) => [item.motion_id, item]));
  const starterIds = featured.selection.starter_question_ids || [];
  const starterQuestions = starterIds.map((id) => poolMap.get(id)).filter(Boolean);
  const starterSet = new Set(starterQuestions.map((item) => item.motion_id));
  return [...starterQuestions, ...pool.filter((item) => !starterSet.has(item.motion_id))];
}

function alignmentArguments(question) {
  if (question.case_for && question.case_against) {
    return { for: question.case_for, against: question.case_against };
  }
  const tradeoff = (question.tradeoff || "").replace(/^Weigh\s+/i, "").replace(/\.$/, "");
  const splitAt = tradeoff.toLocaleLowerCase().indexOf(" against ");
  if (splitAt === -1) {
    return {
      for: `Supporters emphasize the intended benefits: ${tradeoff}.`,
      against: "Opponents question whether those benefits justify the costs, risks or tradeoffs.",
    };
  }
  const supportive = tradeoff.slice(0, splitAt);
  const opposing = tradeoff.slice(splitAt + " against ".length);
  return {
    for: `Supporters emphasize ${supportive}.`,
    against: `Opponents emphasize ${opposing}.`,
  };
}

function stackedBar(favour, opposition, other, label) {
  const total = favour + opposition + other;
  const width = (value) => total ? (value / total) * 100 : 0;
  return `<span class="stacked-bar" role="img" aria-label="${escapeHtml(label)}"><span class="bar-favour" style="width:${width(favour)}%"></span><span class="bar-opposition" style="width:${width(opposition)}%"></span><span class="bar-other" style="width:${width(other)}%"></span></span>`;
}

function formatDate(value) {
  const parsed = new Date(`${value}T12:00:00`);
  return new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "short", day: "numeric" }).format(parsed);
}

function formatMotionDate(value) {
  const parsed = new Date(`${value}T12:00:00`);
  return {
    day: new Intl.DateTimeFormat("en-CA", { month: "short", day: "numeric" }).format(parsed),
    rest: new Intl.DateTimeFormat("en-CA", { year: "numeric" }).format(parsed),
  };
}

function formatNumber(value) { return new Intl.NumberFormat("en-CA").format(value); }
function formatPercent(value) { return `${Math.round(value * 100)}%`; }
function shortName(name) { return name.replace(/^(Councillor|Mayor)\s+/, ""); }
function partyBadge(report) {
  return `<span class="party-badge" style="--party-color:${safeColor(report.party_color)}">${escapeHtml(report.party_short || report.party)}</span>`;
}
function initials(name) {
  const parts = shortName(name).split(/\s+/).filter(Boolean);
  return `${parts[0]?.[0] || ""}${parts.at(-1)?.[0] || ""}`.toUpperCase();
}
function shortVote(vote) {
  if (vote === "In Favour") return "Favour";
  if (vote === "In Opposition") return "Opposed";
  return vote;
}
function safeColor(value) { return /^#[0-9a-f]{6}$/i.test(value || "") ? value : "#7a7f78"; }
function safeExternalUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : "";
  } catch {
    return "";
  }
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character]);
}
function toCamel(value) { return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()); }
