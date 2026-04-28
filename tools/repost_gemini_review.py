import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

GITHUB_API_VERSION = "2022-11-28"
GITHUB_API_URL = "https://api.github.com"
TIMEOUT_SECONDS = 30
_DISMISS_MESSAGE = "Review reposted by automated tool."
JULES_USERNAMES = frozenset(["google-labs-jules", "google-labs-jules[bot]"])
_GRAPHQL_MUTATION_BATCH_SIZE = 50
_VALIDATION_STRING = """CRITICAL:
- Follow AGENTS.md strictly
- Apply pragmatic SOLID and Clean Code principles
- When encountering conflicting review feedback:
  - Select the most coherent and maintainable solution
  - Proceed decisively without any back-and-forth
  - Document the final chosen approach as comment in code
- Run full validation after changes
"""

_GET_THREADS_QUERY = """
query($owner: String!, $name: String!, $pullNumber: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pullNumber) {
      reviewThreads(first: 100, after: $after) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              pullRequestReview {
                databaseId
              }
            }
          }
          lastComments: comments(last: 1) {
            nodes {
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
"""

class RepostError(Exception):
    """Custom exception for errors during the repost process."""
    pass

class GitHubRequestError(Exception):
    """Custom exception for errors during GitHub API requests."""
    pass

class ReviewReposter:
    def __init__(self, token: str, repo: str, pull_number: int):
        self.token = token
        self.repo = repo
        self.pull_number = pull_number

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json"
        }

    def _log_request_error(self, e: Exception, request: urllib.request.Request) -> None:
        url = request.full_url
        if isinstance(e, urllib.error.HTTPError):
            print(f"Error {e.code} {e.reason} for {request.get_method()} {url}", file=sys.stderr)
            print(e.read().decode('utf-8', errors='replace'), file=sys.stderr)
        elif isinstance(e, json.JSONDecodeError):
            print(f"Failed to decode JSON from {url}: {e}", file=sys.stderr)
        elif isinstance(e, urllib.error.URLError):
            print(f"Network error accessing {url}: {e.reason}", file=sys.stderr)
        else:
            print(f"Unexpected error for {url}: {e}", file=sys.stderr)

    def _execute_github_request(self, request: urllib.request.Request) -> Tuple[Optional[Any], Optional[str]]:
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = response.read()
                link_header = response.headers.get('Link')
                next_url = None
                if link_header:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_header)
                    if match:
                        next_url = match.group(1)
                return (json.loads(body) if body else None), next_url
        except Exception as e:
            self._log_request_error(e, request)
            raise GitHubRequestError from e

    def fetch_json(self, url: str) -> Optional[Any]:
        headers = self._get_headers()
        req = urllib.request.Request(url, headers=headers)
        data, next_url = self._execute_github_request(req)

        if not isinstance(data, list):
            return data  # Not a paginated response

        all_data = data
        github_api_netloc = urllib.parse.urlparse(GITHUB_API_URL).netloc
        while next_url:
            # Validate that next_url is within the GITHUB_API_URL domain to prevent SSRF
            if urllib.parse.urlparse(next_url).netloc != github_api_netloc:
                print(f"Warning: Ignoring potentially unsafe pagination link: {next_url}", file=sys.stderr)
                break

            current_url = next_url
            req = urllib.request.Request(current_url, headers=headers)
            page_data, next_url = self._execute_github_request(req)
            if isinstance(page_data, list):
                all_data.extend(page_data)
            else:
                print(f"Warning: Expected a list in paginated response from {current_url}, but got {type(page_data)}.", file=sys.stderr)
                break
        return all_data

    def post_json(self, url: str, data: Dict[str, Any], method: str = "POST") -> Optional[Any]:
        headers = self._get_headers()
        json_data = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=json_data, headers=headers, method=method)
        response_data, _ = self._execute_github_request(req)
        return response_data

    def _execute_graphql(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{GITHUB_API_URL}/graphql"
        payload = {"query": query, "variables": variables or {}}
        # GraphQL is always POST
        return self.post_json(url, payload)

    def dismiss_review(self, review_id: str) -> None:
        """Dismisses the review if it is in a dismissible state."""
        url = f"{self._get_pull_api_url()}/reviews/{review_id}/dismissals"
        payload = {"message": _DISMISS_MESSAGE}
        try:
            self.post_json(url, payload, method="PUT")
            print(f"Dismissed review {review_id}.", file=sys.stderr)
        except GitHubRequestError as e:
            # Log but don't fail the whole process since reposting succeeded
            print(f"Warning: Failed to dismiss review {review_id}: {e}", file=sys.stderr)

    def _fetch_all_review_threads(self) -> List[Dict[str, Any]]:
        """Fetches all review threads for the pull request using pagination."""
        repo_info = self._get_owner_and_name()
        if not repo_info:
            return []
        owner, name = repo_info

        all_threads = []
        cursor = None
        has_next_page = True

        while has_next_page:
            variables = {
                "owner": owner,
                "name": name,
                "pullNumber": self.pull_number,
                "after": cursor
            }
            response = self._execute_graphql(_GET_THREADS_QUERY, variables)

            if not response:
                print(f"Warning: Empty GraphQL response.", file=sys.stderr)
                break

            try:
                review_threads = response["data"]["repository"]["pullRequest"]["reviewThreads"]
            except (KeyError, TypeError):
                print(f"Warning: Could not find 'reviewThreads' in GraphQL response: {response}", file=sys.stderr)
                break

            threads = review_threads.get("nodes", [])
            all_threads.extend(threads)

            page_info = review_threads.get("pageInfo", {})
            has_next_page = page_info.get("hasNextPage", False)
            cursor = page_info.get("endCursor")

        return all_threads

    def _filter_threads_for_review(self, threads: List[Dict[str, Any]], review_db_id: int) -> List[str]:
        """Filters threads to find those belonging to the specific review ID and are unresolved."""
        threads_to_resolve = []
        for thread in threads:
            if thread.get("isResolved"):
                continue

            comments = thread.get("comments", {}).get("nodes", [])
            if not comments:
                continue

            first_comment = comments[0]
            review = first_comment.get("pullRequestReview")
            if review and review.get("databaseId") == review_db_id:
                threads_to_resolve.append(thread["id"])
        return threads_to_resolve

    def _filter_jules_threads(self, threads: List[Dict[str, Any]]) -> List[str]:
        """Filters threads to find those where the last comment is by Jules and are unresolved."""
        threads_to_resolve = []
        for thread in threads:
            if thread.get("isResolved"):
                continue

            last_comments = thread.get("lastComments", {}).get("nodes", [])
            if not last_comments:
                continue

            author_info = last_comments[0].get("author")
            if author_info and author_info.get("login") in JULES_USERNAMES:
                threads_to_resolve.append(thread["id"])
        return threads_to_resolve

    def _get_owner_and_name(self) -> Optional[Tuple[str, str]]:
        parts = self.repo.split("/", 1)
        if len(parts) != 2 or not all(parts):
            print(f"Error: Invalid repo format '{self.repo}'. Cannot resolve threads.", file=sys.stderr)
            return None
        return parts[0], parts[1]

    def _resolve_threads_by_id(self, threads_to_resolve: List[str]) -> None:
        """Resolves the given list of thread IDs in batched GraphQL requests."""
        print(f"Found {len(threads_to_resolve)} threads to resolve.", file=sys.stderr)

        if not threads_to_resolve:
            return

        # GitHub's GraphQL API has a limit on the number of mutations in a single call.
        # A safe limit is around 50. We process threads in batches.
        for i in range(0, len(threads_to_resolve), _GRAPHQL_MUTATION_BATCH_SIZE):
            batch = threads_to_resolve[i:i + _GRAPHQL_MUTATION_BATCH_SIZE]

            mutations = []
            variables = {}
            for j, thread_id in enumerate(batch):
                alias = f"resolve{j}"
                var_name = f"threadId{j}"
                mutations.append(f'  {alias}: resolveReviewThread(input: {{threadId: ${var_name}}}) {{ thread {{ isResolved }} }}')
                variables[var_name] = thread_id

            var_definitions = ", ".join([f"${name}: ID!" for name in variables.keys()])
            mutation_body = "\n".join(mutations)
            batched_mutation = f"mutation({var_definitions}) {{\n{mutation_body}\n}}"

            response = self._execute_graphql(batched_mutation, variables)

            if response and response.get("errors"):
                print(f"Warning: GraphQL errors occurred during batch resolution: {response.get('errors')}", file=sys.stderr)

            if not response or not response.get("data"):
                print(f"Warning: Failed to resolve threads in batch. Response: {response}", file=sys.stderr)
                print(f"Warning: Failed to resolve threads {', '.join(batch)} due to batch failure.", file=sys.stderr)
                continue  # Continue to the next batch

            data = response["data"]
            for j, thread_id in enumerate(batch):
                alias = f"resolve{j}"
                is_resolved = data.get(alias, {}).get("thread", {}).get("isResolved", False)
                if is_resolved:
                    print(f"Resolved thread {thread_id}.", file=sys.stderr)
                else:
                    print(f"Warning: Failed to resolve thread {thread_id}. Response for alias '{alias}': {data.get(alias)}", file=sys.stderr)

    def _get_pull_api_url(self) -> str:
        return f"{GITHUB_API_URL}/repos/{self.repo}/pulls/{self.pull_number}"

    def _fetch_and_prepare_review_meta(self, review_id: str, mention_user: str) -> Tuple[str, str, str]:
        review_url = f"{self._get_pull_api_url()}/reviews/{review_id}"
        review_data = self.fetch_json(review_url)

        if not review_data:
            raise RepostError(f"Failed to fetch review {review_id}. Response was empty.")

        original_body = review_data.get("body") or ""

        # If the original body is just the mention user itself (e.g. from previous runs or
        # empty feedback), treat it as an empty original_body.
        if original_body.strip() == mention_user:
            original_body = ""

        # We don't include the original_body in the new review body to avoid duplication
        # as it will be posted as an issue comment instead.
        # We also don't use just the mention_user to avoid an empty top-level comment.
        new_body = ""
        original_state = review_data.get("state")
        event_map = {"APPROVED": "APPROVE", "CHANGES_REQUESTED": "REQUEST_CHANGES"}
        return new_body, event_map.get(original_state, "COMMENT"), original_body

    def _process_single_comment(self, comment: Dict[str, Any], mention_user: str) -> Optional[Dict[str, Any]]:
        # Prepend mention with a paragraph break
        new_body = f"{mention_user}\n\n{comment['body']}"
        new_comment = {
            "path": comment["path"],
            "body": new_body
        }

        line = comment.get("line") or comment.get("original_line")
        position = comment.get("position") or comment.get("original_position")

        if line:
            new_comment["line"] = line
            new_comment["side"] = comment.get("side", "RIGHT")
        elif position:
            # Fallback to position if line is missing. Do NOT include side.
            new_comment["position"] = position
        else:
            print(f"Warning: Skipping comment on path '{comment['path']}' (no line or position info).", file=sys.stderr)
            return None

        start_line = comment.get("start_line") or comment.get("original_start_line")
        if start_line:
            new_comment["start_line"] = start_line
            if "start_side" in comment:
                new_comment["start_side"] = comment["start_side"]

        return new_comment

    def _fetch_and_prepare_comments(self, review_id: str, mention_user: str) -> List[Dict[str, Any]]:
        url = f"{self._get_pull_api_url()}/reviews/{review_id}/comments"
        comments_data = self.fetch_json(url)

        if not isinstance(comments_data, list):
            if comments_data is not None:
                print(f"Warning: Expected a list of comments, but got {type(comments_data)}. Treating as no comments.", file=sys.stderr)
            comments_data = []

        new_comments = []
        for comment in comments_data:
            processed = self._process_single_comment(comment, mention_user)
            if processed:
                new_comments.append(processed)
        return new_comments

    def _post_issue_comment(self, body: str) -> None:
        url = f"{GITHUB_API_URL}/repos/{self.repo}/issues/{self.pull_number}/comments"
        payload = {"body": body}

        result = self.post_json(url, payload)
        if not result or not result.get('id'):
            raise RepostError("Failed to create issue comment.")

    def _post_new_review(self, payload: Dict[str, Any]) -> None:
        url = f"{self._get_pull_api_url()}/reviews"

        result = self.post_json(url, payload)
        if not result or not result.get('id'):
            raise RepostError("Failed to create review or review ID not found in response.")

    def repost_review(self, review_id: int, mention_user: str) -> None:
        review_id_str = str(review_id)
        _, event, original_body = self._fetch_and_prepare_review_meta(review_id_str, mention_user)
        new_comments = self._fetch_and_prepare_comments(review_id_str, mention_user)

        # Determine if there is anything to post that requires further action (like cleanup).
        has_content_to_post = new_comments or event in ["APPROVE", "REQUEST_CHANGES"]
        if not has_content_to_post:
            return

        # Post issue comment for review description if it exists.
        if original_body.strip():
            issue_comment_body = f"{mention_user}\n\n{original_body}\n\n{_VALIDATION_STRING}"
            self._post_issue_comment(issue_comment_body)

        # Only post a review if there are comments or an approval/request changes event.
        if new_comments or event in ["APPROVE", "REQUEST_CHANGES"]:
            payload = {"event": event}
            if new_comments:
                payload["comments"] = new_comments
            self._post_new_review(payload)

        # Cleanup original review
        try:
            all_threads = self._fetch_all_review_threads()

            threads_to_resolve_ids = set()

            # Collect threads from the original review
            threads_to_resolve_ids.update(self._filter_threads_for_review(all_threads, review_id))

            # Collect threads replied to by Jules
            threads_to_resolve_ids.update(self._filter_jules_threads(all_threads))

            # Resolve all unique threads in one go
            if threads_to_resolve_ids:
                self._resolve_threads_by_id(list(threads_to_resolve_ids))

            if event in ["APPROVE", "REQUEST_CHANGES"]:
                self.dismiss_review(review_id_str)
        except GitHubRequestError as e:
            print(f"Warning: Failed to process review threads: {e}", file=sys.stderr)

def _repo_type(value: str) -> str:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        raise argparse.ArgumentTypeError(f"Invalid repository format: {value}")
    return value

def _mention_user_type(value: str) -> str:
    if not re.fullmatch(r"@[\w.-]+(?:\[bot\])?", value):
        raise argparse.ArgumentTypeError(f"Invalid mention user format: {value}")
    return value

def _parse_args_and_get_token() -> Tuple[argparse.Namespace, str]:
    parser = argparse.ArgumentParser(description="Repost a GitHub PR review with a specific user.")
    parser.add_argument("--repo", required=True, type=_repo_type, help="Format: owner/repo")
    parser.add_argument("--pull-number", required=True, type=int, help="Numeric pull request ID")
    parser.add_argument("--review-id", required=True, type=int, help="Numeric review ID")
    parser.add_argument("--mention-user", default="@jules", type=_mention_user_type, help="User to mention (e.g. @jules)")
    parser.add_argument("--token", help="GitHub PAT. Defaults to the USER_PAT environment variable.")
    args = parser.parse_args()

    token = args.token or os.environ.get("USER_PAT")
    if not token:
        print("Error: The USER_PAT environment variable or --token argument must be set.", file=sys.stderr)
        sys.exit(1)
    return args, token

def main() -> None:
    args, token = _parse_args_and_get_token()
    try:
        reposter = ReviewReposter(token, args.repo, args.pull_number)
        reposter.repost_review(args.review_id, args.mention_user)
    except RepostError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except GitHubRequestError:
        # Error was already logged by _log_request_error.
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
