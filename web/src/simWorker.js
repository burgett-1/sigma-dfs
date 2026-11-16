// Runs the heavy simulations off the main thread so the page stays responsive.
import { buildSim, simulateContest } from "./engine.js";
import { runLateSwap } from "./lateswap.js";

self.onmessage = (ev) => {
  const { id, type, payload } = ev.data;
  const progress = (f) => self.postMessage({ id, progress: f });
  try {
    if (type === "single") {
      const { players, lineup, opts } = payload;
      const sim = buildSim(players, opts.now || Date.now());
      const [res] = simulateContest(sim, players, [lineup], opts, progress);
      self.postMessage({ id, result: res });
    } else if (type === "lateswap") {
      const { players, doc, opts } = payload;
      self.postMessage({ id, result: runLateSwap(players, doc, opts, progress) });
    }
  } catch (e) {
    self.postMessage({ id, error: String(e && e.message || e) });
  }
};
