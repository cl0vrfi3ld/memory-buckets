# The built-in embedding model (ADR-0015): minishlab/potion-retrieval-32M at the
# revision static_model.py pins, converted to float16 by the plugin's own stdlib
# converter. That halves it to ~65 MB with cosine differences around 2e-5. Wheels
# built outside Nix get the same files from hatch_build.py.
{
  lib,
  fetchurl,
  runCommand,
  python,
}:

let
  revision = "6fc8051fab2a1e0ee76689cf08c853792ac285e7";
  fetch =
    name: sha256:
    fetchurl {
      url = "https://huggingface.co/minishlab/potion-retrieval-32M/resolve/${revision}/${name}";
      inherit sha256;
    };
  upstream = {
    # Also pinned in static_model.py (MODEL_FILES) for runtime downloads.
    model = fetch "model.safetensors" "07609e5bd33aad37900b3fd62f4ec96f6daec88ca4d46b9d8b928bfababf6ea0";
    vocab = fetch "vocab.txt" "4b3452e69455f96c6cfc1cdb212d3b7b1a3e9d2505ab6f61a50022f61467a6a3";
    # Only for the tokenizer parity test; the plugin never reads it.
    tokenizer = fetch "tokenizer.json" "7d75cbc54318138807c401b0f0c9721117c628b39de8e8e0edb6cb17e0ee7d18";
  };
in
runCommand "potion-retrieval-32M-f16"
  {
    nativeBuildInputs = [ python ];
    passthru = { inherit upstream revision; };
    meta = {
      description = "potion-retrieval-32M static embedding model (MinishLab), float16";
      homepage = "https://huggingface.co/minishlab/potion-retrieval-32M";
      license = lib.licenses.mit;
    };
  }
  ''
    mkdir -p $out
    cp ${upstream.vocab} $out/vocab.txt
    python3 - ${../memory_buckets/static_model.py} ${upstream.model} $out <<'PY'
    import importlib.util, pathlib, sys
    spec = importlib.util.spec_from_file_location("static_model", sys.argv[1])
    sm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sm)
    out = pathlib.Path(sys.argv[3])
    sm.convert_to_f16(sys.argv[2], out / "model.safetensors")
    (out / "NOTICE").write_text(sm.notice())
    PY
  ''
