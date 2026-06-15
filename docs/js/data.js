// SOCBench results data: extracted from RESULTS_2026-06-04.md.
// All numbers cite the 1,205 eval units shared across providers (anthropic
// ran 1,205; openai/gemini ran 1,500; reported subset is the shared 1,205).

window.SOCBenchData = {
  runs: {
    anthropic: {
      runId: "20260604T011942Z_full_main_anthropic_4bc5181b_632cbb",
      units: 1205, renderings: 4814, costUsd: 724.82, model: "claude-opus-4-7",
      color: "#d97706",
    },
    openai: {
      runId: "20260604T012213Z_full_main_openai_4bc5181b_0b1f4c",
      units: 1500, renderings: 6000, costUsd: 328.59, model: "gpt-5.4",
      color: "#059669",
    },
    gemini: {
      runId: "20260604T012407Z_full_main_gemini_4bc5181b_e4a9c6",
      units: 1500, renderings: 6000, costUsd: 355.13, model: "gemini-2.5-pro",
      color: "#7c3aed",
    },
  },

  scope: {
    sharedUnits: 1205,
    benign: 458,
    malicious: 376,
    mixed: 371,
  },

  // §1b best persona per provider (winner by per-flow F1 on combined)
  bestPersona: [
    { provider: "anthropic", persona: "soc_analyst",     verdictAcc: 0.9163, verdictF1: 0.9261, flowF1: 0.6528, pairF1: 0.6158, hostF1: 0.7981, costUsd: 98.66,  wallMs: 21931.9 },
    { provider: "gemini",    persona: "soc_analyst",     verdictAcc: 0.8412, verdictF1: 0.8780, flowF1: 0.5217, pairF1: 0.4886, hostF1: 0.7911, costUsd: 57.90,  wallMs: 20349.0 },
    { provider: "openai",    persona: "threat_analyst",  verdictAcc: 0.8430, verdictF1: 0.8630, flowF1: 0.4441, pairF1: 0.4263, hostF1: 0.5306, costUsd: 81.65,  wallMs: 21326.3 },
  ],

  // §1a mean of all four personas, per split
  meanPerProvider: {
    combined: [
      { provider: "anthropic", fpv: 0.8966, verdictAcc: 0.8426, verdictF1: 0.8812, flowF1: 0.536, pairF1: 0.5081, hostF1: 0.6663, conf: 0.7899, costUsd: 724.82, wallMs: 37539, fpr: 0.3697 },
      { provider: "gemini", fpv: 0.9386, verdictAcc: 0.7816, verdictF1: 0.8429, flowF1: 0.4061, pairF1: 0.3842, hostF1: 0.5822, conf: 0.9203, costUsd: 296.52, wallMs: 30165, fpr: 0.4928 },
      { provider: "openai", fpv: 0.9878, verdictAcc: 0.7273, verdictF1: 0.8025, flowF1: 0.3171, pairF1: 0.2975, hostF1: 0.4279, conf: 0.8632, costUsd: 272.33, wallMs: 18739, fpr: 0.5168 },
    
    ],
    benign: [
      { provider: "anthropic", fpv: 0.9913, verdictAcc: 0.6306, verdictF1: null, flowF1: 0.6362, pairF1: 0.6362, hostF1: 0.6362, conf: 0.6287, costUsd: 145.85, wallMs: 25399, fpr: 0.3697 },
      { provider: "gemini", fpv: 0.9814, verdictAcc: 0.5071, verdictF1: null, flowF1: 0.5671, pairF1: 0.5671, hostF1: 0.5671, conf: 0.864, costUsd: 76.05, wallMs: 24470, fpr: 0.4928 },
      { provider: "openai", fpv: 0.9885, verdictAcc: 0.4829, verdictF1: null, flowF1: 0.5061, pairF1: 0.5061, hostF1: 0.5061, conf: 0.7742, costUsd: 37.53, wallMs: 13728, fpr: 0.5168 },
    
    ],
    malicious: [
      { provider: "anthropic", fpv: 0.8385, verdictAcc: 0.9924, verdictF1: 0.9962, flowF1: 0.4585, pairF1: 0.4097, hostF1: 0.685, conf: 0.9076, costUsd: 578.96, wallMs: 44993, fpr: null },
      { provider: "gemini", fpv: 0.9123, verdictAcc: 0.964, verdictF1: 0.9817, flowF1: 0.3003, pairF1: 0.264, hostF1: 0.5937, conf: 0.9575, costUsd: 220.47, wallMs: 33657, fpr: null },
      { provider: "openai", fpv: 0.9873, verdictAcc: 0.8771, verdictF1: 0.9336, flowF1: 0.2008, pairF1: 0.1691, hostF1: 0.3795, conf: 0.9177, costUsd: 234.8, wallMs: 21811, fpr: null },
    
    ],
    mixed: [
      { provider: "anthropic", fpv: 0.7741, verdictAcc: 0.989, verdictF1: 0.9945, flowF1: 0.2101, pairF1: 0.2048, hostF1: 0.4535, conf: 0.9241, costUsd: 281.53, wallMs: 45479, fpr: null },
      { provider: "gemini", fpv: 0.9333, verdictAcc: 0.9683, verdictF1: 0.9838, flowF1: 0.1367, pairF1: 0.1299, hostF1: 0.5157, conf: 0.9735, costUsd: 105.04, wallMs: 34540, fpr: null },
      { provider: "openai", fpv: 0.9892, verdictAcc: 0.9442, verdictF1: 0.9711, flowF1: 0.0782, pairF1: 0.0736, hostF1: 0.2041, conf: 0.956, costUsd: 102.73, wallMs: 21559, fpr: null },
    
    ],
  },

  // §1c per-persona × provider on combined split
  perPersona: [
    { provider: "anthropic", persona: "adversary_hunter", fpv: 0.961, verdictAcc: 0.8453, verdictF1: 0.8863, flowF1: 0.5126, pairF1: 0.4834, hostF1: 0.6845, conf: 0.7985, costUsd: 193.19, p50: 39012, p95: 74179 },
    { provider: "anthropic", persona: "detection_engineer", fpv: 0.906, verdictAcc: 0.8044, verdictF1: 0.8564, flowF1: 0.4967, pairF1: 0.4785, hostF1: 0.6097, conf: 0.818, costUsd: 226.38, p50: 45530, p95: 83100 },
    { provider: "anthropic", persona: "soc_analyst", fpv: 0.7934, verdictAcc: 0.9163, verdictF1: 0.9261, flowF1: 0.6528, pairF1: 0.6158, hostF1: 0.7981, conf: 0.7516, costUsd: 98.66, p50: 22308, p95: 35940 },
    { provider: "anthropic", persona: "threat_analyst", fpv: 0.926, verdictAcc: 0.8043, verdictF1: 0.856, flowF1: 0.4821, pairF1: 0.4546, hostF1: 0.5727, conf: 0.7917, costUsd: 206.59, p50: 36920, p95: 70321 },
    { provider: "gemini", persona: "adversary_hunter", fpv: 0.9751, verdictAcc: 0.8051, verdictF1: 0.8611, flowF1: 0.4446, pairF1: 0.4157, hostF1: 0.594, conf: 0.9106, costUsd: 57.45, p50: 25508, p95: 49266 },
    { provider: "gemini", persona: "detection_engineer", fpv: 0.971, verdictAcc: 0.8051, verdictF1: 0.858, flowF1: 0.4169, pairF1: 0.4011, hostF1: 0.5641, conf: 0.9333, costUsd: 65.91, p50: 27214, p95: 57630 },
    { provider: "gemini", persona: "soc_analyst", fpv: 0.9095, verdictAcc: 0.8412, verdictF1: 0.878, flowF1: 0.5217, pairF1: 0.4886, hostF1: 0.7911, conf: 0.9269, costUsd: 57.9, p50: 18250, p95: 37473 },
    { provider: "gemini", persona: "threat_analyst", fpv: 0.8988, verdictAcc: 0.675, verdictF1: 0.7744, flowF1: 0.2412, pairF1: 0.2312, hostF1: 0.3795, conf: 0.9104, costUsd: 115.26, p50: 28659, p95: 68452 },
    { provider: "openai", persona: "adversary_hunter", fpv: 0.9917, verdictAcc: 0.6636, verdictF1: 0.7587, flowF1: 0.2761, pairF1: 0.2594, hostF1: 0.3967, conf: 0.8503, costUsd: 65.02, p50: 14859, p95: 23139 },
    { provider: "openai", persona: "detection_engineer", fpv: 0.9925, verdictAcc: 0.6739, verdictF1: 0.7794, flowF1: 0.2502, pairF1: 0.2276, hostF1: 0.3813, conf: 0.8966, costUsd: 84.48, p50: 15923, p95: 28890 },
    { provider: "openai", persona: "soc_analyst", fpv: 0.9784, verdictAcc: 0.7286, verdictF1: 0.8088, flowF1: 0.2979, pairF1: 0.2766, hostF1: 0.4029, conf: 0.8635, costUsd: 41.17, p50: 9150, p95: 21518 },
    { provider: "openai", persona: "threat_analyst", fpv: 0.9884, verdictAcc: 0.843, verdictF1: 0.863, flowF1: 0.4441, pairF1: 0.4263, hostF1: 0.5306, conf: 0.8425, costUsd: 81.65, p50: 14789, p95: 23904 },
  
  ],

  // §2 reliability + cost rollup per provider
  reliability: [
    { provider: "anthropic", fpv: 0.8966, defects: 0, totalUsd: 724.82, p50WallMs: 35942, p95WallMs: 65885 },
    { provider: "gemini", fpv: 0.9386, defects: 0, totalUsd: 296.52, p50WallMs: 24908, p95WallMs: 53205 },
    { provider: "openai", fpv: 0.9878, defects: 0, totalUsd: 272.33, p50WallMs: 13680, p95WallMs: 24363 },
  
  ],
};
