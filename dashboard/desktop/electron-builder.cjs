module.exports = {
  appId: 'dev.thehomie.desktop',
  productName: 'The Homie Desktop',
  artifactName: 'The-Homie-Desktop-${version}-${arch}.${ext}',
  directories: {
    output: 'dist',
  },
  files: [
    'main.cjs',
    'preload.cjs',
    'lib/**/*',
    'renderer/**/*',
    'package.json',
  ],
  extraResources: [
    {
      from: '../web/dist',
      to: 'dashboard-web',
      filter: ['**/*'],
    },
  ],
  asar: true,
  npmRebuild: false,
  publish: null,
  win: {
    requestedExecutionLevel: 'asInvoker',
    signAndEditExecutable: false,
  },
};
