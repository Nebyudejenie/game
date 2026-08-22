// A player's 5x5 card -- render-once + mutate, same discipline as the
// call board (render/board.js).

const HEADER = ["B", "I", "N", "G", "O"];

let cells = []; // cells[row][col]
let currentGrid = null;

export function buildCard(container) {
  container.innerHTML = "";
  cells = [];

  const header = document.createElement("div");
  header.className = "mini-card-header";
  for (const letter of HEADER) {
    const cell = document.createElement("div");
    cell.textContent = letter;
    header.appendChild(cell);
  }

  const grid = document.createElement("div");
  grid.className = "mini-card";
  for (let r = 0; r < 5; r++) {
    const rowCells = [];
    for (let c = 0; c < 5; c++) {
      const cell = document.createElement("div");
      cell.className = "card-cell";
      grid.appendChild(cell);
      rowCells.push(cell);
    }
    cells.push(rowCells);
  }

  container.appendChild(header);
  container.appendChild(grid);
}

export function setCardGrid(grid) {
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

export function markCalledOnCard(calledSet) {
  if (!currentGrid) return;
  for (let r = 0; r < 5; r++) {
    for (let c = 0; c < 5; c++) {
      const value = currentGrid[r][c];
      if (value === 0) continue;
      cells[r][c].classList.toggle("marked", calledSet.has(value));
    }
  }
}

export function hasCompletePattern(calledSet, patterns) {
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

export function onCellClick(handler) {
  for (let r = 0; r < 5; r++) {
    for (let c = 0; c < 5; c++) {
      cells[r][c].addEventListener("click", () => handler(r, c));
    }
  }
}
