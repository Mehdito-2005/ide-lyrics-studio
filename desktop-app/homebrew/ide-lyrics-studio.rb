cask "ide-lyrics-studio" do
  version "1.8.4.1"
  sha256 :no_check

  url "https://github.com/Mehdito-2005/ide-lyrics-studio/releases/download/v#{version}/IDE-Lyrics-Studio-macOS-arm64.dmg"
  name "IDE Lyrics Studio"
  desc "3D IDE-style synced lyrics desktop app, live wallpaper, and integrations hub"
  homepage "https://github.com/Mehdito-2005/ide-lyrics-studio"

  depends_on arch: :arm64
  depends_on macos: ">= :ventura"

  app "IDE-Lyrics-Studio.app"

  postflight do
    system_command "/usr/bin/xattr",
                   args: ["-cr", "#{appdir}/IDE-Lyrics-Studio.app"],
                   sudo: false
  end

  zap trash: [
    "~/Library/Application Support/IDELyricsStudio",
    "~/Library/LaunchAgents/com.idelyrics.studio.plist",
  ]
end
