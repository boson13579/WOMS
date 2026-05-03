/**
 * ESLint configuration — Airbnb + airbnb-typescript with strict TS rules.
 *
 * Per docs/RULES.md §2: enforce Airbnb React/JSX Style Guide via ESLint Strict.
 * This config layers Airbnb's base over @typescript-eslint's strict-type-checked
 * preset and adds React/Hooks/JSX-A11y rules.
 */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  extends: [
    'airbnb',
    'airbnb/hooks',
    'airbnb-typescript',
    'plugin:@typescript-eslint/strict-type-checked',
    'plugin:@typescript-eslint/stylistic-type-checked',
    'plugin:react/recommended',
    'plugin:react/jsx-runtime',
    'plugin:react-hooks/recommended',
    'plugin:jsx-a11y/recommended',
    'plugin:import/recommended',
    'plugin:import/typescript',
    'plugin:prettier/recommended',
  ],
  ignorePatterns: [
    'dist',
    'node_modules',
    '.eslintrc.cjs',
    'vite.config.ts',
    'tailwind.config.ts',
    'postcss.config.js',
  ],
  parser: '@typescript-eslint/parser',
  parserOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    project: './tsconfig.json',
    tsconfigRootDir: __dirname,
  },
  plugins: ['react-refresh', 'react', 'react-hooks', '@typescript-eslint', 'import'],
  settings: {
    react: { version: 'detect' },
    'import/resolver': {
      typescript: { project: './tsconfig.json' },
      node: true,
    },
  },
  rules: {
    /* React 17+ JSX transform — `import React` not required. */
    'react/react-in-jsx-scope': 'off',
    'react/require-default-props': 'off',

    /* HMR boundary check (Vite). */
    'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],

    /* Allow .tsx files to declare JSX. */
    'react/jsx-filename-extension': ['error', { extensions: ['.tsx'] }],

    /* Default exports OK for Pages / Route components per Bulletproof React. */
    'import/prefer-default-export': 'off',

    /* Type-only imports keep bundle slim. */
    '@typescript-eslint/consistent-type-imports': [
      'error',
      { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
    ],

    /* Unused-vars: ignore underscore-prefixed args (matches mypy convention). */
    '@typescript-eslint/no-unused-vars': [
      'error',
      { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
    ],

    /* Import order — group by origin. */
    'import/order': [
      'error',
      {
        groups: ['builtin', 'external', 'internal', 'parent', 'sibling', 'index'],
        'newlines-between': 'always',
        alphabetize: { order: 'asc', caseInsensitive: true },
        pathGroups: [{ pattern: '@/**', group: 'internal' }],
      },
    ],

    /* Prop spreading is core to React composition (Radix, shadcn forwardRef
     * components, RHF `register` returns). Disable Airbnb's strict ban — it
     * fights idiomatic React. */
    'react/jsx-props-no-spreading': 'off',

    /* Don't fail when a component file co-exports its variant table — that's
     * the shadcn convention (`Button` + `buttonVariants`). */
    'react-refresh/only-export-components': 'off',

    /* Allow `void promise` to mark intentionally-ignored async results.
     * Required when `@typescript-eslint/no-floating-promises` is on. */
    'no-void': ['error', { allowAsStatement: true }],

    /* Numbers in template literals are universally understood and safe. */
    '@typescript-eslint/restrict-template-expressions': [
      'error',
      { allowNumber: true, allowBoolean: true, allowNullish: false },
    ],

    /* Function declarations are hoisted — flagging "used before defined" for
     * helpers placed below the main component is unhelpful noise. */
    'no-use-before-define': 'off',
    '@typescript-eslint/no-use-before-define': [
      'error',
      { functions: false, classes: false, variables: true, typedefs: false },
    ],

    /* Test/config files may use devDependencies. */
    'import/no-extraneous-dependencies': [
      'error',
      {
        devDependencies: [
          '**/*.test.{ts,tsx}',
          '**/*.spec.{ts,tsx}',
          '**/test/**',
          'vite.config.ts',
          'tailwind.config.ts',
          'postcss.config.js',
        ],
      },
    ],
  },
};
