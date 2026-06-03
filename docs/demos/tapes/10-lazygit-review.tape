Output docs/demos/gifs/10-lazygit-review.gif

Require zsh
Require python3
Require git
Require lazygit

Set Shell "zsh"
Set FontSize 18
Set FontFamily "Berkeley Mono"
Set Width 1280
Set Height 760
Set Padding 28
Set Theme "Rose Pine"
Set TypingSpeed 22ms
Set Framerate 18
Set PlaybackSpeed 1.05
Set WindowBar Colorful

Hide
Type "export PS1='$ '"
Enter
Type "source docs/demos/setup.zsh 10-lazygit-review"
Enter
Type "sigil_demo_make_parser_change"
Enter
Type "clear"
Enter
Show

Type "lazygit"
Enter
Sleep 4500ms
Type "q"
Sleep 700ms
Type "git status --short"
Enter
Sleep 900ms
Type ", review the working tree changes for risk"
Enter
Sleep 2600ms
