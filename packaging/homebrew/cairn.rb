class Cairn < Formula
  include Language::Python::Virtualenv

  desc "Daily new-grad job feed: fetch postings, rank them, track applications"
  homepage "https://github.com/druhinb/cairn"
  url "https://github.com/druhinb/cairn/archive/refs/tags/v0.0.2.tar.gz"
  sha256 "<sha256 of the tarball>"
  license "AGPL-3.0-or-later"

  # pydantic-core, which FastAPI needs, builds from its Rust source
  depends_on "rust" => :build
  depends_on :macos
  depends_on "python@3.14"

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Ranking and summaries need a model provider. Pick one in the app under
      Settings › AI provider, or with `cairn llm set <provider>`:
        claude-code  Claude Code on PATH, logged in: https://claude.com/claude-code
        anthropic    Anthropic API key
        gemini       Google Gemini API key, free tier
        groq         Groq API key, free tier
        mistral      Mistral API key, free Experiment plan
        openrouter   OpenRouter API key, free models
        ollama       Ollama running on this Mac, no key
        custom       any OpenAI-compatible API
      Start with:
        cairn init
        cairn ui
    EOS
  end

  test do
    assert_match "cairn", shell_output("#{bin}/cairn --help")
    ENV["CAIRN_HOME"] = testpath/"home"
    system bin/"cairn", "init"
    assert_path_exists testpath/"home/config.toml"
  end
end
