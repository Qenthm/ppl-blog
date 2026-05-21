# ppl-blog

Personal engineering blog for PPL (Software Engineering Lab) individual review. Built with Hugo + PaperMod theme, deployed on Vercel.

## Purpose & audience

Posts here serve as evidence for PPL individual review competences (M1–M6 mandatory + electives E1–E5). Each post should be credible to a **professional software engineer** reading it — meaning:

- Assume the reader knows what a database, a test suite, a CI pipeline, and a deployment are. Don't explain basics.
- Go deep on the technical specifics: exact tool versions, actual code snippets, real numbers (benchmark results, query times, line counts), real error messages.
- The post should give a professional programmer something useful or interesting — a technique, a mental model, a lesson from a real failure — not just a summary of what was done.
- Skip motivational framing ("this is important because…"). Get to the technical substance fast.
- Write in first person as an engineer who did the work and has opinions about it.

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
