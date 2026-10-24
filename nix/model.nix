# The built-in embedding model: onnx-community/embeddinggemma-2-ONNX (q4) at the
# revision embeddings.py pins. Only the text backbone and tokeniser are fetched;
# vision and audio encoders live in separate .onnx files and are not used. Wheels
# built outside Nix get the same files from hatch_build.py.
{
  lib,
  fetchurl,
  runCommand,
  python,
}:

let
  revision = "daa72c51243991dfcaf9f9137d2c573d8f7790c0";
  fetch =
    name: sha256:
    fetchurl {
      url = "https://huggingface.co/onnx-community/embeddinggemma-2-ONNX/resolve/${revision}/${name}";
      inherit sha256;
    };
  upstream = {
    # Also pinned in embeddings.py (MODEL_FILES) for runtime downloads.
    model = fetch "onnx/model_q4.onnx" "f9eeba97acddf139b8ee2ddf04bc30dceafa88de93fadf74d7644e0d61a477a9";
    model_data = fetch "onnx/model_q4.onnx_data" "c3975f2d1ab7a1878ae31a7d7a9b7804a827aff3800b60dfceafce21cac3df49";
    tokenizer = fetch "tokenizer.json" "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4";
  };
in
runCommand "embeddinggemma-2-ONNX-q4"
  {
    nativeBuildInputs = [ python ];
    passthru = { inherit upstream revision; };
    meta = {
      description = "EmbeddingGemma 2 ONNX (q4) text embedding model";
      homepage = "https://huggingface.co/onnx-community/embeddinggemma-2-ONNX";
      license = lib.licenses.asl20;
    };
  }
  ''
    mkdir -p $out
    cp ${upstream.model} $out/model_q4.onnx
    cp ${upstream.model_data} $out/model_q4.onnx_data
    cp ${upstream.tokenizer} $out/tokenizer.json
    python3 - ${../memory_buckets/embeddings.py} $out <<'PY'
    import importlib.util, pathlib, sys
    spec = importlib.util.spec_from_file_location("embeddings", sys.argv[1])
    emb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emb)
    (pathlib.Path(sys.argv[2]) / "NOTICE").write_text(emb.notice(), encoding="utf-8")
    PY
  ''
