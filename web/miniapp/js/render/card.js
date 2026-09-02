// A player's Bingo card(s) -- render-once + mutate, same discipline as the
// call board (render/board.js). createCard() is a factory, not a
// singleton: a player can hold more than one card in the same round now
// (spec: configurable via room.max_cards_per_player), and each held card
// gets its own independent instance with its own cells/grid, so marking
// one card's numbers or checking its own win state can never bleed into
// a different card's.

const HEADER = ["B", "I", "N", "G", "O"];

// Pattern-name -> exact [row, col] cells, mirroring packages/core/
// bingo.py's own _all_patterns() naming exactly (row_{r}, col_{c},
// diag_main, diag_anti, corners) -- safe to duplicate here since it's a
// stable, purely presentational derivation (which cells to draw a ring
// around), never a source of truth for whether a claim is actually
// valid, which stays entirely server-side.
function cellsForPattern(name) {
  if (name.startsWith("row_")) {
    const r = Number(name.slice(4));
    return [0, 1, 2, 3, 4].map((c) => [r, c]);
  }
  if (name.startsWith("col_")) {
    const c = Number(name.slice(4));
    return [0, 1, 2, 3, 4].map((r) => [r, c]);
  }
  if (name === "diag_main") return [0, 1, 2, 3, 4].map((i) => [i, i]);
  if (name === "diag_anti") return [0, 1, 2, 3, 4].map((i) => [i, 4 - i]);
  if (name === "corners") return [[0, 0], [0, 4], [4, 0], [4, 4]];
  return [];
}

// A one-off, stateless render for the result screen's winning-card
// preview -- deliberately not built on createCard()'s own per-instance
// state below, which belongs to a live, interactively-updated game card
// and must never be silently repurposed by a screen that only ever needs
// one static snapshot.
export function renderStaticCard(container, grid, calledNumbers, winningPattern) {
  container.innerHTML = "";
  const calledSet = new Set(calledNumbers || []);
  const winningCells = new Set(cellsForPattern(winningPattern || "").map(([r, c]) => `${r},${c}`));

  const header = document.createElement("div");
  header.className = "mini-card-header";
  for (const letter of HEADER) {
    const cell = document.createElement("div");
    cell.textContent = letter;
    header.appendChild(cell);
  }

  const body = document.createElement("div");
  body.className = "mini-card";
  for (let r = 0; r < 5; r++) {
    for (let c = 0; c < 5; c++) {
      const value = grid[r][c];
      const cell = document.createElement("div");
      const isFree = value === 0;
      const isCalled = isFree || calledSet.has(value);
      const isWinning = winningCells.has(`${r},${c}`);
      cell.className = [
        "card-cell",
        isFree ? "free" : "",
        isCalled ? "marked" : "",
        isWinning ? "winning" : "",
      ]
        .filter(Boolean)
        .join(" ");
      cell.textContent = isFree ? "★" : String(value);
      body.appendChild(cell);
    }
  }

  container.appendChild(header);
  container.appendChild(body);
}

// The live, interactive card -- one instance per card a player holds.
// container is wiped and rebuilt fresh (matches the old buildCard()'s own
// behavior); every returned method closes over this instance's own
// cells/currentGrid, never a module-level singleton, so N held cards
// never contend with each other.
export function createCard(container) {
  container.innerHTML = "";
  const cells = []; // cells[row][col]
  let currentGrid = null;

  const header = document.createElement("div");
  header.className = "mini-card-header";
  for (const letter of HEADER) {
    const cell = document.createElement("div");
    cell.textContent = letter;
    header.appendChild(cell);
  }

  const gridEl = document.createElement("div");
  gridEl.className = "mini-card";
  for (let r = 0; r < 5; r++) {
    const rowCells = [];
    for (let c = 0; c < 5; c++) {
      const cell = document.createElement("div");
      cell.className = "card-cell";
      gridEl.appendChild(cell);
      rowCells.push(cell);
    }
    cells.push(rowCells);
  }

  container.appendChild(header);
  container.appendChild(gridEl);

  function setGrid(grid) {
    currentGrid = grid;
    for (let r = 0; r < 5; r++) {
      for (let c = 0; c < 5; c++) {
        const value = grid[r][c];
        const cell = cells[r][c];
        if (value === 0) {
          cell.textContent = "★";
          cell.className = "card-cell free marked";
        } else {
          cell.textContent = String(value);
          cell.className = "card-cell";
        }
      }
    }
  }

  function markCalled(calledSet) {
    if (!currentGrid) return;
    for (let r = 0; r < 5; r++) {
      for (let c = 0; c < 5; c++) {
        const value = currentGrid[r][c];
        if (value === 0) continue;
        cells[r][c].classList.toggle("marked", calledSet.has(value));
      }
    }
  }

  function hasCompletePattern(calledSet, patterns) {
    if (!currentGrid) return false;
    const marked = (r, c) => currentGrid[r][c] === 0 || calledSet.has(currentGrid[r][c]);

    if (patterns.includes("row")) {
      for (let r = 0; r < 5; r++) {
        if ([0, 1, 2, 3, 4].every((c) => marked(r, c))) return true;
      }
    }
    if (patterns.includes("col")) {
      for (let c = 0; c < 5; c++) {
        if ([0, 1, 2, 3, 4].every((r) => marked(r, c))) return true;
      }
    }
    if (patterns.includes("diag")) {
      if ([0, 1, 2, 3, 4].every((i) => marked(i, i))) return true;
      if ([0, 1, 2, 3, 4].every((i) => marked(i, 4 - i))) return true;
    }
    if (patterns.includes("corners")) {
      if (marked(0, 0) && marked(0, 4) && marked(4, 0) && marked(4, 4)) return true;
    }
    return false;
  }

  // tabindex="0" + role="button" + a keydown handler -- the same
  // reachable-without-a-pointer fix app.js's own makeKeyboardActivatable()
  // applies to the room list/card-selection grid/AUTO toggle. Only
  // meaningful when AUTO is off (handler no-ops otherwise) -- manual
  // marking is advisory only, the server never trusts a client-reported
  // mark either way.
  function onCellClick(handler) {
    for (let r = 0; r < 5; r++) {
      for (let c = 0; c < 5; c++) {
        const cell = cells[r][c];
        cell.tabIndex = 0;
        cell.setAttribute("role", "button");
        cell.addEventListener("click", () => handler(r, c));
        cell.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handler(r, c);
          }
        });
      }
    }
  }

  return { setGrid, markCalled, hasCompletePattern, onCellClick };
}
