const path = require("path");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Allow importing shared catalogs from ../../packages/catalogs
  experimental: { externalDir: true },
  transpilePackages: ["pptx-preview"],
  webpack: (config) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      "@aop-catalogs": path.join(__dirname, "../../packages/catalogs"),
    };
    return config;
  },
  async redirects() {
    return [
      { source: "/favicon.ico", destination: "/favicon.svg", permanent: false },
    ];
  },
};

module.exports = nextConfig;
