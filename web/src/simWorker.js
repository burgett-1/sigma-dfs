// Runs the heavy simulations off the main thread so the page stays responsive.
import { buildSim, simulateContest } from "./engine.js";
import { runLateSwap } from "./lateswap.js";
import { generatePool } from "./poolgen.js";

self.onmessage = (ev) => {
  const { id, type, payload } = ev.data;
  const progress = (f) => self.postMessage({ id, progress: f });
  try {
    if (type === "single") {
      const { players, lineup, opts } = payload;
      const sim = buildSim(players, opts.now || Date.now(), opts.spec || undefined);   // spec: sports.js specFor
      const [res] = simulateContest(sim, players, [lineup], opts, progress);
      self.postMessage({ id, result: res });
    } else if (type === "lateswap") {
      const { players, doc, opts } = payload;
      self.postMessage({ id, result: runLateSwap(players, doc, opts, progress) });
    } else if (type === "pool") {
      const { players, opts } = payload;
      self.postMessage({ id, result: generatePool(players, opts, progress) });
    }
  } catch (e) {
    self.postMessage({ id, error: String(e && e.message || e) });
  }
};
