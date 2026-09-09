/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  transpilePackages: ["@puddingai/knowledge-platform-console-contracts"],
};

export default nextConfig;
