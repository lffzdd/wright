// Existing violations are recorded in eslint-suppressions.json; new code must pass.
// After fixing old code, run `npm run lint -- --prune-suppressions` so the baseline only shrinks.
import js from "@eslint/js";
import stylistic from "@stylistic/eslint-plugin";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "qa", "test-results", "playwright-report"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: { ecmaVersion: 2022, globals: { ...globals.browser } },
    plugins: { "react-hooks": reactHooks, "@stylistic": stylistic },
    rules: {
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "error",
      "no-nested-ternary": "error",
      "@stylistic/max-len": ["error", {
        code: 120,
        ignoreUrls: true,
        ignoreStrings: true,
        ignoreTemplateLiterals: true,
        ignoreRegExpLiterals: true,
      }],
      "max-lines": ["error", { max: 400, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    // Tests, fixtures and copy tables are long by nature.
    files: ["**/*.test.{ts,tsx}", "e2e/**", "src/i18n.ts", "src/types.ts"],
    rules: { "max-lines": "off" },
  },
);
