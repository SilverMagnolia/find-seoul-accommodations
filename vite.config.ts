import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const repositoryName = process.env.GITHUB_REPOSITORY?.split("/")[1];
const githubPagesBase =
  repositoryName != null ? `/${repositoryName}/` : "/";

export default defineConfig({
  base: process.env.GITHUB_ACTIONS === "true" ? githubPagesBase : "/",
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173
  }
});
