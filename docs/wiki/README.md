# Wiki source pages

These Markdown files are the reviewed source for the shared library’s GitHub Wiki. Edit them through the repository’s normal PR process. Publish deliberately when the documentation is needed; ordinary pushes and app releases must not trigger Wiki publication.

`Home.md` is the landing page, `Workflow-index.md` groups the workflow pages, and `_Sidebar.md` supplies Wiki navigation. Keep the individual explanations short and link the existing operating guide and generated caller contract instead of duplicating those references.

Use relative `.md` links for other pages in this folder and repository-relative links for the detailed guides and workflow sources. The publisher converts those destinations for the Wiki. The source README explains maintenance and is not itself a published Wiki page.

When a workflow is added, removed or renamed, update its page, the grouped index and sidebar in the same PR. Document what really exists and which product-specific setup remains. Do not publish credentials, private account information or invented deployment results.

Validate the source links with the existing documentation check before publication:

```sh
python3 -B -m shared_ci.docs --config .ci/docs.json --root .
```
