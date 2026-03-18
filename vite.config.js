var _a;
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
var repositoryName = (_a = process.env.GITHUB_REPOSITORY) === null || _a === void 0 ? void 0 : _a.split("/")[1];
var githubPagesBase = repositoryName != null ? "/".concat(repositoryName, "/") : "/";
export default defineConfig({
    base: process.env.GITHUB_ACTIONS === "true" ? githubPagesBase : "/",
    plugins: [react()],
    server: {
        host: "0.0.0.0",
        port: 5173
    }
});
