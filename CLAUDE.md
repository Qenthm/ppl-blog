# ppl-blog

Personal engineering blog for PPL coursework. Built with Hugo + PaperMod theme, deployed on Vercel.

## Stack

- Static site generator: Hugo 0.161.1 (Extended)
- Theme: PaperMod (git submodule at `themes/PaperMod`)
- Hosting: Vercel — https://ppl-blog-qenthms-projects.vercel.app
- Repo: https://github.com/Qenthm/ppl-blog

## Key commands

```powershell
# Preview locally
hugo server

# Deploy to production
vercel --prod --yes
```

Hugo binary location (if not on PATH after fresh shell):
`C:\Users\Rifqi\AppData\Local\Microsoft\WinGet\Packages\Hugo.Hugo.Extended_Microsoft.Winget.Source_8wekyb3d8bbwe\hugo.exe`

## Writing a new post

```powershell
hugo new posts/my-post-title.md
# Edit content/posts/my-post-title.md
# Set draft: false when ready to publish
git add . && git commit -m "post: my post title" && git push
vercel --prod --yes
```

## Structure

```
content/
  posts/     ← blog posts go here
  about.md   ← about page
hugo.toml    ← site config (baseURL, theme, nav menu)
vercel.json  ← pins Hugo version, sets buildCommand and outputDirectory
themes/
  PaperMod/  ← theme (git submodule, do not edit directly)
```

## Notes

- GitHub auto-deploy is not connected — deploy manually with `vercel --prod --yes`
- The `public/` directory is built by Vercel on deploy; do not commit it
- PaperMod theme deprecation warnings in build output are from the theme itself, not our config — safe to ignore
