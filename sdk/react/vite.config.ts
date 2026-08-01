import { defineConfig } from "vite";
import dts from "vite-plugin-dts";

// Library build: externalize React so the host app provides it.
export default defineConfig({
  plugins: [dts({ include: ["src"], rollupTypes: true })],
  build: {
    lib: {
      entry: "src/index.ts",
      name: "GraphifyReact",
      fileName: "graphify-react",
      formats: ["es", "umd"],
    },
    rollupOptions: {
      external: ["react", "react-dom", "react/jsx-runtime"],
      output: {
        globals: { react: "React", "react-dom": "ReactDOM", "react/jsx-runtime": "jsxRuntime" },
      },
    },
  },
});
