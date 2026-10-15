// Static analysis for the web UI (QUALITY.md: Power of Ten rules 1, 4, 6, 7, 10; zero findings).
// Run: npx --yes eslint@9.39.1 minutenest/webapp/static
// No node_modules in this Python project: @eslint/js is loaded from the ESLint that runs.
import { createRequire } from "node:module";

const js = createRequire(process.argv[1])("@eslint/js");

export default [
  js.configs.recommended,
  {
    files: ["minutenest/webapp/static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        window: "readonly", document: "readonly", console: "readonly", fetch: "readonly",
        setTimeout: "readonly", clearTimeout: "readonly", setInterval: "readonly", clearInterval: "readonly",
        requestAnimationFrame: "readonly", cancelAnimationFrame: "readonly", localStorage: "readonly",
        EventSource: "readonly", XMLHttpRequest: "readonly", FormData: "readonly", URLSearchParams: "readonly",
        Event: "readonly", CustomEvent: "readonly", MutationObserver: "readonly", ResizeObserver: "readonly",
        HTMLSelectElement: "readonly", HTMLElement: "readonly", DataTransfer: "readonly", File: "readonly",
        Blob: "readonly", navigator: "readonly", location: "readonly", performance: "readonly",
        getComputedStyle: "readonly", matchMedia: "readonly", crypto: "readonly", AbortController: "readonly",
        enhanceSelect: "readonly",
      },
    },
    rules: {
      // Power of Ten 4: a function fits on a page
      "max-lines-per-function": ["error", { max: 60, skipBlankLines: true, skipComments: true }],
      // Power of Ten 1: simple control flow
      "complexity": ["error", 15],
      "max-depth": ["error", 4],
      "no-labels": "error",
      // Power of Ten 6/7: smallest scope, nothing silently ignored
      "no-var": "error",
      "prefer-const": "error",
      "no-unused-vars": ["error", { args: "after-used", caughtErrors: "none" }],
      "no-empty": ["error", { allowEmptyCatch: false }],
      "no-shadow": "error",
      "eqeqeq": ["error", "always"],
    },
  },
];
