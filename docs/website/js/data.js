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
      // LogLM: encoder-only foundation model. No LLM turns, so fpv (completion) is N/A.
      // costUsd is total for the 1,205 shared units at ~$0.00004/alert.
      { provider: "loglm", fpv: null, verdictAcc: null, verdictF1: 0.954, flowF1: 0.989, pairF1: null, hostF1: null, conf: null, costUsd: 0.20, wallMs: null, fpr: 0.02 },
      { provider: "anthropic", fpv: 0.8966, verdictAcc: 0.8426, verdictF1: 0.8812, flowF1: 0.536, pairF1: 0.5081, hostF1: 0.6663, conf: 0.7899, costUsd: 724.82, wallMs: 37539, fpr: 0.3697 },
      { provider: "gemini", fpv: 0.9386, verdictAcc: 0.7816, verdictF1: 0.8429, flowF1: 0.4061, pairF1: 0.3842, hostF1: 0.5822, conf: 0.9203, costUsd: 296.52, wallMs: 30165, fpr: 0.4928 },
      { provider: "openai", fpv: 0.9878, verdictAcc: 0.7273, verdictF1: 0.8025, flowF1: 0.3171, pairF1: 0.2975, hostF1: 0.4279, conf: 0.8632, costUsd: 272.33, wallMs: 18739, fpr: 0.5168 },
      // OSS on the 1,205 shared subset. costUsd = effective compute-inclusive cost, scaled 1205/1500 from the dump.
      { provider: "foundation-sec", fpv: 0.7268, verdictAcc: 0.6418, verdictF1: 0.7535, flowF1: 0.3635, pairF1: 0.3629, hostF1: 0.3667, conf: null, costUsd: 61.75, wallMs: null, fpr: 0.7561 },
      { provider: "seneca", fpv: 0.6678, verdictAcc: 0.7005, verdictF1: 0.7036, flowF1: 0.4551, pairF1: 0.4513, hostF1: 0.5335, conf: null, costUsd: 110.44, wallMs: null, fpr: 0.3818 },
      { provider: "glm", fpv: 0.3662, verdictAcc: 0.5976, verdictF1: 0.4855, flowF1: 0.5509, pairF1: 0.5485, hostF1: 0.5837, conf: null, costUsd: 1065.22, wallMs: null, fpr: 0.4792, coverage: 0.27 },
    ],
    benign: [
      // LogLM per-split: projected from sequence-grain metrics onto SOCBench eval-unit prior.
      { provider: "loglm", fpv: null, verdictAcc: 0.980, verdictF1: null, flowF1: 0.980, pairF1: 0.980, hostF1: 0.980, conf: null, costUsd: 0.08, wallMs: null, fpr: 0.02 },
      { provider: "anthropic", fpv: 0.9913, verdictAcc: 0.6306, verdictF1: null, flowF1: 0.6362, pairF1: 0.6362, hostF1: 0.6362, conf: 0.6287, costUsd: 145.85, wallMs: 25399, fpr: 0.3697 },
      { provider: "gemini", fpv: 0.9814, verdictAcc: 0.5071, verdictF1: null, flowF1: 0.5671, pairF1: 0.5671, hostF1: 0.5671, conf: 0.864, costUsd: 76.05, wallMs: 24470, fpr: 0.4928 },
      { provider: "openai", fpv: 0.9885, verdictAcc: 0.4829, verdictF1: null, flowF1: 0.5061, pairF1: 0.5061, hostF1: 0.5061, conf: 0.7742, costUsd: 37.53, wallMs: 13728, fpr: 0.5168 },
      { provider: "foundation-sec", fpv: 0.7107, verdictAcc: 0.2439, verdictF1: null, flowF1: 0.9660, pairF1: 0.9660, hostF1: 0.9660, conf: null, costUsd: 23.48, wallMs: null, fpr: 0.7561 },
      { provider: "seneca", fpv: 0.8750, verdictAcc: 0.6182, verdictF1: null, flowF1: 0.8049, pairF1: 0.8049, hostF1: 0.8049, conf: null, costUsd: 41.98, wallMs: null, fpr: 0.3818 },
      { provider: "glm", fpv: 0.5955, verdictAcc: 0.5208, verdictF1: null, flowF1: 0.5291, pairF1: 0.5291, hostF1: 0.5291, conf: null, costUsd: 404.89, wallMs: null, fpr: 0.4792, coverage: 0.27 },
    ],
    malicious: [
      { provider: "loglm", fpv: null, verdictAcc: 0.922, verdictF1: 0.960, flowF1: 0.989, pairF1: 0.989, hostF1: 0.989, conf: null, costUsd: 0.06, wallMs: null, fpr: null },
      { provider: "anthropic", fpv: 0.8385, verdictAcc: 0.9924, verdictF1: 0.9962, flowF1: 0.4585, pairF1: 0.4097, hostF1: 0.685, conf: 0.9076, costUsd: 578.96, wallMs: 44993, fpr: null },
      { provider: "gemini", fpv: 0.9123, verdictAcc: 0.964, verdictF1: 0.9817, flowF1: 0.3003, pairF1: 0.264, hostF1: 0.5937, conf: 0.9575, costUsd: 220.47, wallMs: 33657, fpr: null },
      { provider: "openai", fpv: 0.9873, verdictAcc: 0.8771, verdictF1: 0.9336, flowF1: 0.2008, pairF1: 0.1691, hostF1: 0.3795, conf: 0.9177, costUsd: 234.8, wallMs: 21811, fpr: null },
      { provider: "foundation-sec", fpv: 0.7340, verdictAcc: 0.8643, verdictF1: 0.9258, flowF1: 0.0103, pairF1: 0.0083, hostF1: 0.0122, conf: null, costUsd: 19.27, wallMs: null, fpr: null },
      { provider: "seneca", fpv: 0.5485, verdictAcc: 0.7029, verdictF1: 0.8143, flowF1: 0.1611, pairF1: 0.1444, hostF1: 0.2534, conf: null, costUsd: 34.46, wallMs: null, fpr: null },
      { provider: "glm", fpv: 0.1689, verdictAcc: 0.9750, verdictF1: 0.9868, flowF1: 0.7562, pairF1: 0.7499, hostF1: 0.8905, conf: null, costUsd: 332.35, wallMs: null, fpr: null, coverage: 0.27 },
    ],
    mixed: [
      { provider: "loglm", fpv: null, verdictAcc: 0.922, verdictF1: 0.960, flowF1: 0.989, pairF1: 0.989, hostF1: 0.989, conf: null, costUsd: 0.06, wallMs: null, fpr: null },
      { provider: "anthropic", fpv: 0.7741, verdictAcc: 0.989, verdictF1: 0.9945, flowF1: 0.2101, pairF1: 0.2048, hostF1: 0.4535, conf: 0.9241, costUsd: 281.53, wallMs: 45479, fpr: null },
      { provider: "gemini", fpv: 0.9333, verdictAcc: 0.9683, verdictF1: 0.9838, flowF1: 0.1367, pairF1: 0.1299, hostF1: 0.5157, conf: 0.9735, costUsd: 105.04, wallMs: 34540, fpr: null },
      { provider: "openai", fpv: 0.9892, verdictAcc: 0.9442, verdictF1: 0.9711, flowF1: 0.0782, pairF1: 0.0736, hostF1: 0.2041, conf: 0.956, costUsd: 102.73, wallMs: 21559, fpr: null },
      { provider: "foundation-sec", fpv: 0.7392, verdictAcc: 0.8886, verdictF1: 0.9398, flowF1: 0.0040, pairF1: 0.0041, hostF1: 0.0123, conf: null, costUsd: 19.01, wallMs: null, fpr: null },
      { provider: "seneca", fpv: 0.5330, verdictAcc: 0.8212, verdictF1: 0.8940, flowF1: 0.0452, pairF1: 0.0451, hostF1: 0.2940, conf: null, costUsd: 34.00, wallMs: null, fpr: null },
      { provider: "glm", fpv: 0.1163, verdictAcc: 0.8177, verdictF1: 0.8936, flowF1: 0.5032, pairF1: 0.4913, hostF1: 0.6958, conf: null, costUsd: 327.98, wallMs: null, fpr: null, coverage: 0.27 },
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
    // OSS: aggregated on the 1,205 shared subset. costUsd = effective (compute-inclusive) prorated per-persona from the API-cost ratio.
    { provider: "foundation-sec", persona: "adversary_hunter", fpv: 0.7485, verdictAcc: 0.6718, verdictF1: 0.7901, flowF1: 0.3459, pairF1: 0.3459, hostF1: 0.3459, conf: 0.9018, costUsd: 16.75, p50: 26144, p95: 138443 },
    { provider: "foundation-sec", persona: "detection_engineer", fpv: 0.7178, verdictAcc: 0.6393, verdictF1: 0.7539, flowF1: 0.3653, pairF1: 0.3641, hostF1: 0.3676, conf: 0.8770, costUsd: 23.85, p50: 33295, p95: 154650 },
    { provider: "foundation-sec", persona: "soc_analyst", fpv: 0.6929, verdictAcc: 0.6335, verdictF1: 0.7268, flowF1: 0.3653, pairF1: 0.3643, hostF1: 0.3737, conf: 0.8354, costUsd: 7.10, p50: 24031, p95: 74824 },
    { provider: "foundation-sec", persona: "threat_analyst", fpv: 0.7477, verdictAcc: 0.6226, verdictF1: 0.7432, flowF1: 0.3775, pairF1: 0.3774, hostF1: 0.3796, conf: 0.8820, costUsd: 14.05, p50: 28456, p95: 165707 },
    { provider: "seneca", persona: "adversary_hunter", fpv: 0.7710, verdictAcc: 0.7513, verdictF1: 0.7990, flowF1: 0.4038, pairF1: 0.4036, hostF1: 0.4284, conf: 0.7369, costUsd: 30.32, p50: 98613, p95: 689335 },
    { provider: "seneca", persona: "detection_engineer", fpv: 0.7303, verdictAcc: 0.7284, verdictF1: 0.7801, flowF1: 0.4137, pairF1: 0.4123, hostF1: 0.4364, conf: 0.7706, costUsd: 31.15, p50: 104540, p95: 752779 },
    { provider: "seneca", persona: "soc_analyst", fpv: 0.5975, verdictAcc: 0.7222, verdictF1: 0.7143, flowF1: 0.5147, pairF1: 0.5044, hostF1: 0.6736, conf: 0.7365, costUsd: 20.65, p50: 86021, p95: 331963 },
    { provider: "seneca", persona: "threat_analyst", fpv: 0.5726, verdictAcc: 0.6000, verdictF1: 0.5208, flowF1: 0.4881, pairF1: 0.4849, hostF1: 0.5957, conf: 0.7205, costUsd: 28.32, p50: 103161, p95: 728204 },
    { provider: "glm", persona: "adversary_hunter", fpv: 0.3427, verdictAcc: 0.6455, verdictF1: 0.4800, flowF1: 0.6093, pairF1: 0.6035, hostF1: 0.6273, conf: 0.7674, costUsd: 303.34, p50: 63989, p95: 329285 },
    { provider: "glm", persona: "detection_engineer", fpv: 0.4299, verdictAcc: 0.6522, verdictF1: 0.5789, flowF1: 0.5442, pairF1: 0.5337, hostF1: 0.6232, conf: 0.7784, costUsd: 384.23, p50: 73105, p95: 420861 },
    { provider: "glm", persona: "soc_analyst", fpv: 0.3578, verdictAcc: 0.7350, verdictF1: 0.5974, flowF1: 0.7106, pairF1: 0.7109, hostF1: 0.7265, conf: 0.7761, costUsd: 115.99, p50: 31015, p95: 172262 },
    { provider: "glm", persona: "threat_analyst", fpv: 0.3344, verdictAcc: 0.3578, verdictF1: 0.2857, flowF1: 0.3396, pairF1: 0.3459, hostF1: 0.3578, conf: 0.7350, costUsd: 261.66, p50: 44065, p95: 191667 },
    // LogLM: encoder-only, no personas. Single point on the scatter. costUsd is total on 1,205 subset.
    { provider: "loglm", persona: null, fpv: null, verdictAcc: null, verdictF1: 0.954, flowF1: 0.989, pairF1: 0.989, hostF1: 0.989, conf: null, costUsd: 0.20, p50: null, p95: null },
  ],

  // §2 reliability + cost rollup per provider
  reliability: [
    { provider: "anthropic", fpv: 0.8966, defects: 0, totalUsd: 724.82, p50WallMs: 35942, p95WallMs: 65885 },
    { provider: "gemini", fpv: 0.9386, defects: 0, totalUsd: 296.52, p50WallMs: 24908, p95WallMs: 53205 },
    { provider: "openai", fpv: 0.9878, defects: 0, totalUsd: 272.33, p50WallMs: 13680, p95WallMs: 24363 },
  
  ],
};
