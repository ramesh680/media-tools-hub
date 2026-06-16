const state = {
  allMovies: [],
  filteredMovies: [],
  range: null,
  summary: null,
};

const els = {
  dateForm: document.getElementById("dateForm"),
  startDate: document.getElementById("startDate"),
  endDate: document.getElementById("endDate"),
  refreshButton: document.getElementById("refreshButton"),
  searchInput: document.getElementById("searchInput"),
  genreFilter: document.getElementById("genreFilter"),
  scaleFilter: document.getElementById("scaleFilter"),
  typeFilter: document.getElementById("typeFilter"),
  distributorFilter: document.getElementById("distributorFilter"),
  resetFilters: document.getElementById("resetFilters"),
  exportCsv: document.getElementById("exportCsv"),
  moviesBody: document.getElementById("moviesBody"),
  emptyState: document.getElementById("emptyState"),
  statusText: document.getElementById("statusText"),
  warningText: document.getElementById("warningText"),
  totalMovies: document.getElementById("totalMovies"),
  wideMovies: document.getElementById("wideMovies"),
  limitedMovies: document.getElementById("limitedMovies"),
  dateSpan: document.getElementById("dateSpan"),
  sourceBadge: document.getElementById("sourceBadge"),
};

function toDateInputValue(date) {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function addMonths(date, months) {
  const next = new Date(date);
  const originalDay = next.getDate();
  next.setMonth(next.getMonth() + months);
  if (next.getDate() !== originalDay) {
    next.setDate(0);
  }
  return next;
}

function setPreset(months) {
  const today = new Date();
  els.startDate.value = toDateInputValue(today);
  els.endDate.value = toDateInputValue(addMonths(today, months));
  document.querySelectorAll(".preset-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.months === String(months));
  });
}

function setStatus(message, warning = "") {
  els.statusText.textContent = message;
  els.warningText.textContent = warning;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function loadMovies({ refresh = false } = {}) {
  const params = new URLSearchParams({
    start_date: els.startDate.value,
    end_date: els.endDate.value,
  });
  if (refresh) {
    params.set("refresh", "1");
  }

  setStatus("Loading upcoming release movies...");
  els.moviesBody.innerHTML = '<tr class="loading-row"><td colspan="8">Loading releases from Box Office Mojo...</td></tr>';
  els.emptyState.hidden = true;

  const response = await fetch(`/api/upcoming-release-movies?${params.toString()}`);
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Unable to load upcoming release movies.");
  }

  state.allMovies = payload.movies || [];
  state.range = payload.range;
  state.summary = payload.summary;
  updateSummary(payload);
  updateFilterOptions();
  applyFilters();
  const warning = payload.warnings && payload.warnings.length ? `${payload.warnings.length} source warning(s)` : "";
  setStatus(`Showing ${state.filteredMovies.length} of ${state.allMovies.length} upcoming release movies`, warning);
  els.sourceBadge.textContent = `Fetched ${new Date(payload.fetched_at).toLocaleString()}`;
}

function updateSummary(payload) {
  const scales = payload.summary?.release_scales || {};
  els.totalMovies.textContent = payload.summary?.total_movies ?? 0;
  els.wideMovies.textContent = scales.Wide || 0;
  els.limitedMovies.textContent = scales.Limited || 0;
  els.dateSpan.textContent = `${payload.range.start_date} to ${payload.range.end_date}`;
}

function uniqueSorted(values) {
  return [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
}

function fillSelect(select, values, firstLabel) {
  const current = select.value;
  select.innerHTML = `<option value="">${firstLabel}</option>`;
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  if (values.includes(current)) {
    select.value = current;
  }
}

function updateFilterOptions() {
  fillSelect(els.genreFilter, uniqueSorted(state.allMovies.flatMap((movie) => movie.genres || [])), "All genres");
  fillSelect(els.scaleFilter, uniqueSorted(state.allMovies.map((movie) => movie.release_scale)), "All scales");
  fillSelect(els.typeFilter, uniqueSorted(state.allMovies.map((movie) => movie.release_type)), "All types");
  fillSelect(els.distributorFilter, uniqueSorted(state.allMovies.map((movie) => movie.distributor_network)), "All distributors");
}

function applyFilters() {
  const search = els.searchInput.value.trim().toLowerCase();
  const genre = els.genreFilter.value;
  const scale = els.scaleFilter.value;
  const type = els.typeFilter.value;
  const distributor = els.distributorFilter.value;

  state.filteredMovies = state.allMovies.filter((movie) => {
    const searchable = [
      movie.title,
      movie.tt_code,
      movie.distributor_network,
      movie.genre,
      movie.cast,
      movie.release_type,
      movie.release_scale,
      movie.metacritic_url,
    ].join(" ").toLowerCase();
    return (
      (!search || searchable.includes(search)) &&
      (!genre || (movie.genres || []).includes(genre)) &&
      (!scale || movie.release_scale === scale) &&
      (!type || movie.release_type === type) &&
      (!distributor || movie.distributor_network === distributor)
    );
  });

  renderMovies();
  setStatus(`Showing ${state.filteredMovies.length} of ${state.allMovies.length} upcoming release movies`, els.warningText.textContent);
}

function renderMovies() {
  els.emptyState.hidden = state.filteredMovies.length > 0;
  els.moviesBody.innerHTML = state.filteredMovies.map(renderMovieRow).join("");
}

function renderMovieRow(movie) {
  const poster = movie.poster_url
    ? `<img class="poster" src="${escapeHtml(movie.poster_url)}" alt="">`
    : '<div class="poster" aria-hidden="true"></div>';
  const titleLink = movie.box_office_mojo_url || movie.imdb_url || "#";
  const tt = movie.tt_code ? `<div class="subtle">${escapeHtml(movie.tt_code)}</div>` : '<div class="subtle">No tt code listed</div>';
  const genres = (movie.genres || []).map((genre) => `<span class="tag">${escapeHtml(genre)}</span>`).join("");
  const note = movie.release_note ? `<div class="subtle">${escapeHtml(movie.release_note)}</div>` : "";
  const links = [
    movie.box_office_mojo_url ? `<a href="${escapeHtml(movie.box_office_mojo_url)}" target="_blank" rel="noopener">BOM</a>` : "",
    movie.imdb_url ? `<a href="${escapeHtml(movie.imdb_url)}" target="_blank" rel="noopener">IMDb</a>` : "",
    movie.imdb_pro_url ? `<a href="${escapeHtml(movie.imdb_pro_url)}" target="_blank" rel="noopener">IMDbPro</a>` : "",
    movie.metacritic_url ? `<a href="${escapeHtml(movie.metacritic_url)}" target="_blank" rel="noopener">Metacritic</a>` : "",
  ].filter(Boolean).join("");

  return `
    <tr>
      <td><strong>${escapeHtml(movie.release_date_display || movie.release_date)}</strong></td>
      <td>
        <div class="movie-cell">
          ${poster}
          <div>
            <a class="title" href="${escapeHtml(titleLink)}" target="_blank" rel="noopener">${escapeHtml(movie.title)}</a>
            ${tt}
          </div>
        </div>
      </td>
      <td>
        <strong>${escapeHtml(movie.distributor_network || "N/A")}</strong>
        ${movie.distributor_url ? `<div class="subtle">Company link available</div>` : ""}
      </td>
      <td><div class="tag-row">${genres || '<span class="subtle">N/A</span>'}</div></td>
      <td>
        <div class="tag-row">
          <span class="tag type">${escapeHtml(movie.release_type || "Unknown")}</span>
          <span class="tag scale">${escapeHtml(movie.release_scale || "Unknown")}</span>
        </div>
        ${note}
      </td>
      <td>${escapeHtml(movie.cast || "N/A")}</td>
      <td>${escapeHtml(movie.runtime || "N/A")}</td>
      <td><div class="link-list">${links || '<span class="subtle">N/A</span>'}</div></td>
    </tr>
  `;
}

function exportCsv() {
  const headers = [
    "release_date",
    "title",
    "tt_code",
    "distributor_network",
    "genre",
    "release_type",
    "release_scale",
    "cast",
    "runtime",
    "box_office_mojo_url",
    "imdb_url",
    "imdb_pro_url",
    "metacritic_url",
  ];
  const lines = [headers.join(",")];
  state.filteredMovies.forEach((movie) => {
    lines.push(headers.map((header) => csvCell(movie[header])).join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `upcoming-release-movies-${els.startDate.value}-to-${els.endDate.value}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function csvCell(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

document.querySelectorAll(".preset-button").forEach((button) => {
  button.addEventListener("click", () => {
    setPreset(Number(button.dataset.months));
    loadMovies().catch((error) => setStatus(error.message));
  });
});

els.dateForm.addEventListener("submit", (event) => {
  event.preventDefault();
  document.querySelectorAll(".preset-button").forEach((button) => button.classList.remove("active"));
  loadMovies().catch((error) => setStatus(error.message));
});

els.refreshButton.addEventListener("click", () => {
  loadMovies({ refresh: true }).catch((error) => setStatus(error.message));
});

[els.searchInput, els.genreFilter, els.scaleFilter, els.typeFilter, els.distributorFilter].forEach((control) => {
  control.addEventListener("input", applyFilters);
});

els.resetFilters.addEventListener("click", () => {
  els.searchInput.value = "";
  els.genreFilter.value = "";
  els.scaleFilter.value = "";
  els.typeFilter.value = "";
  els.distributorFilter.value = "";
  applyFilters();
});

els.exportCsv.addEventListener("click", exportCsv);

setPreset(18);
loadMovies().catch((error) => setStatus(error.message));
