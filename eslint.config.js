// eslint.config.js
const sdl = require("@microsoft/eslint-plugin-sdl");

module.exports = [
  {
    ignores: [
      "lib/lp/app/javascript/ellipsis.js",
      "lib/lp/app/javascript/gallery-accordion/gallery-accordion.js",
      "lib/lp/app/javascript/mustache.js",
      "lib/lp/app/javascript/sorttable/sorttable.js",
      "build/",
      "env/"
    ]
  },
  ...sdl.configs.recommended,
  {
    rules: {
      // Codebase sanitizes via Y.Escape.html() before DOM writes
      "@microsoft/sdl/no-html-method": "off"
    }
  },
  {
    files: ["**/tests/**/*.js"],
    rules: {
      // Test DOM setup uses innerHTML with hardcoded strings, no XSS risk.
      "@microsoft/sdl/no-inner-html": "off",
      // Test fixtures use http:// URLs that are not real endpoints.
      "@microsoft/sdl/no-insecure-url": "off",
    }
  }
];
