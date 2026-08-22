// The 75-number call-history board. Built ONCE; every update after that
// mutates only the className of the one cell that changed (spec section 4:
// "Never re-render the whole board per call -- on a low-end Android that
// alone will drop frames").

const COLUMNS = ["B", "I", "N", "G", "O"];
const ROWS = 15;

let cellsByNumber = new Map();
let yourCardNumbers = new Set();

export function buildBoard(container) {
  container.innerHTML = "";
  cellsByNumber = new Map();

  const header = document.createElement("div");
  header.className = "board-header";
  for (const letter of COLUMNS) {
    const cell = document.createElement("div");
    cell.textContent = letter;
    header.appendChild(cell);
  }

  const grid = document.createElement("div");
  grid.className = "board";
  // Row-major DOM order over a 5-column CSS grid: row 0 becomes
  // [1, 16, 31, 46, 61], row 1 becomes [2, 17, 32, 47, 62], etc. --
  // matching the spec's own mockup layout exactly.
  for (let row = 0; row < ROWS; row++) {
    for (let col = 0; col < COLUMNS.length; col++) {
      const number = col * ROWS + row + 1;
      const cell = document.createElement("div");
      cell.className = "board-cell";
      cell.textContent = String(number);
      grid.appendChild(cell);
      cellsByNumber.set(number, cell);
    }
  }

  container.appendChild(header);
  container.appendChild(grid);
}

export function setYourCardNumbers(numbers) {
  yourCardNumbers = new Set(numbers);
  for (const [number, cell] of cellsByNumber) {
    if (cell.classList.contains("called")) {
      cell.classList.toggle("near", yourCardNumbers.has(number));
    }
  }
}

export function markCalled(number) {
  const cell = cellsByNumber.get(number);
  if (!cell) return;
  cell.classList.add("called");
  if (yourCardNumbers.has(number)) cell.classList.add("near");
}

export function markAllCalled(numbers) {
  for (const number of numbers) markCalled(number);
}

export function resetBoard() {
  for (const cell of cellsByNumber.values()) {
    cell.classList.remove("called", "near");
  }
}
