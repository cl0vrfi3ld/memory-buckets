# The plugin as a Python package. Hermes finds it through the
# `hermes_agent.memory_providers` entry point once it's on Hermes's PYTHONPATH
# (services.hermes-agent.extraPythonPackages).
#
# Build it with Hermes's own interpreter, or Hermes can't import it:
#   config.services.hermes-agent.package.python.pkgs.callPackage ./nix/package.nix { }
#
# The built-in embedding model (nix/model.nix) is bundled at memory_buckets/model/,
# where the plugin looks first, so it never downloads one at runtime. It's copied in
# before the build: hatch_build.py uses a model it finds there instead of fetching one.
{
  lib,
  callPackage,
  buildPythonPackage,
  hatchling,
  pytestCheckHook,
  python,
  model ? callPackage ./model.nix { },
}:

let
  pyproject = lib.importTOML ../pyproject.toml;
in
buildPythonPackage {
  pname = pyproject.project.name;
  inherit (pyproject.project) version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../LICENSE
      ../plugin.yaml
      ../__init__.py
      ../cli.py
      ../hatch_build.py
      ../skills
      ../tests
      (lib.fileset.fileFilter (f: f.hasExt "py") ../memory_buckets)
    ];
  };

  preBuild = ''
    cp -r ${model} memory_buckets/model
    chmod -R u+w memory_buckets/model
  '';

  build-system = [ hatchling ];
  dependencies = [ ]; # stdlib only

  postInstall = ''
    test -f $out/${python.sitePackages}/memory_buckets/model/model.safetensors
  '';

  nativeCheckInputs = [ pytestCheckHook ];
  # The real-model tests run against the bundled model and its float32 original. The
  # tokenizer parity test also needs HF tokenizers (the flake's `tokenizer` check adds
  # it). The Hermes integration tests need Hermes's interpreter and skip themselves.
  preCheck = ''
    export HOME=$TMPDIR MEMORY_BUCKETS_EMBEDDINGS_AUTO_DOWNLOAD=0
    export MEMORY_BUCKETS_MODEL_DIR=${model}
    export MEMORY_BUCKETS_MODEL_F32=${model.upstream.model}
    export MEMORY_BUCKETS_REFERENCE_TOKENIZER=${model.upstream.tokenizer}
  '';
  pythonImportsCheck = [ "memory_buckets" ];

  passthru = {
    inherit model;
    # For skills.external_dirs, which turns the bundled sort-inbox skill into /sort-inbox.
    skillsPath = "${python.sitePackages}/memory_buckets/skills";
  };

  meta = {
    inherit (pyproject.project) description;
    license = lib.licenses.mit;
  };
}
