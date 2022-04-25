# Copyright Contributors to the Packit project.
# SPDX-License-Identifier: MIT

import logging
from typing import Any, Iterable, Optional, Union, Callable, List, Tuple, Dict

from ogr.abstract import GitProject

from packit.config.job_config import JobConfig
from packit.exceptions import PackitException
from packit_service.config import ServiceConfig
from packit_service.constants import FAQ_URL
from packit_service.models import AllowlistModel, AllowlistStatus
from packit_service.worker.events import (
    EventData,
    AbstractCoprBuildEvent,
    InstallationEvent,
    IssueCommentEvent,
    IssueCommentGitlabEvent,
    KojiTaskEvent,
    MergeRequestCommentGitlabEvent,
    MergeRequestGitlabEvent,
    PullRequestCommentGithubEvent,
    PullRequestCommentPagureEvent,
    PullRequestGithubEvent,
    PullRequestPagureEvent,
    PushGitHubEvent,
    PushGitlabEvent,
    PushPagureEvent,
    ReleaseEvent,
    TestingFarmResultsEvent,
    CheckRerunEvent,
)
from packit_service.worker.events.koji import KojiBuildEvent
from packit_service.worker.helpers.build import CoprBuildJobHelper
from packit_service.worker.reporting import BaseCommitStatus

logger = logging.getLogger(__name__)

UncheckedEvent = Union[
    PushPagureEvent,
    PullRequestPagureEvent,
    PullRequestCommentPagureEvent,
    AbstractCoprBuildEvent,
    TestingFarmResultsEvent,
    InstallationEvent,
    KojiTaskEvent,
    KojiBuildEvent,
    CheckRerunEvent,
]


class Allowlist:
    @staticmethod
    def _strip_protocol_and_add_git(url: Optional[str]) -> Optional[str]:
        """
        Remove the protocol from the URL and add .git suffix.

        Args:
            url (Optional[str]): URL to remove protocol from and add .git suffix to.

        Returns:
            URL without the protocol with added .git suffix. If not given URL returns
            None.
        """
        if not url:
            return None
        return url.split("://")[1] + ".git"

    @staticmethod
    def add_namespace(namespace: str) -> bool:
        """
        Add namespace to the allowlist with `waiting` status if it is not in there already.

        Args:
            namespace (str): Namespace to be added in format of: `github.com/namespace`
                or `github.com/namespace/repo.git`.

        Returns:
            `True` if account is already in our allowlist. `False` otherwise.
        """
        if AllowlistModel.get_namespace(namespace):
            return True

        AllowlistModel.add_namespace(namespace, AllowlistStatus.waiting.value)
        return False

    @staticmethod
    def approve_namespace(namespace: str):
        """
        Approve namespace manually.

        Args:
            namespace (str): Namespace in the format of `github.com/namespace` or
                `github.com/namespace/repository.git`.
        """
        AllowlistModel.add_namespace(
            namespace=namespace, status=AllowlistStatus.approved_manually.value
        )

        logger.info(f"Account {namespace!r} approved successfully.")

    @staticmethod
    def is_approved(namespace: str) -> bool:
        """
        Checks if namespace is approved in the allowlist.

        Args:
            namespace (str): Namespace in format `example.com/namespace/repository.git`,
                where `/repository.git` is optional.

        Returns:
            `True` if namespace is approved, `False` otherwise.
        """
        if not namespace:
            return False

        separated_path = [namespace, None]
        while len(separated_path) > 1:
            if matching_namespace := AllowlistModel.get_namespace(separated_path[0]):
                status = AllowlistStatus(matching_namespace.status)
                if status != AllowlistStatus.waiting:
                    return status in (
                        AllowlistStatus.approved_automatically,
                        AllowlistStatus.approved_manually,
                    )

            separated_path = separated_path[0].rsplit("/", 1)

        logger.info(f"Could not find entry for: {namespace}")
        return False

    @staticmethod
    def remove_namespace(namespace: str) -> bool:
        """
        Remove namespace from the allowlist.

        Args:
            namespace (str): Namespace to be removed in format of `github.com/namespace`
                or `github.com/namespace/repository.git` if for specific repository.

        Returns:
            `True` if the namespace was in the allowlist before, `False` otherwise.
        """
        if not AllowlistModel.get_namespace(namespace):
            logger.info(f"Namespace {namespace!r} does not exist!")
            return False

        AllowlistModel.remove_namespace(namespace)
        logger.info(f"Namespace {namespace!r} removed from allowlist!")

        return True

    @staticmethod
    def waiting_namespaces() -> List[str]:
        """
        Get namespaces waiting for approval.

        Returns:
            List of namespaces that are waiting for approval.
        """
        return [
            account.namespace
            for account in AllowlistModel.get_namespaces_by_status(
                AllowlistStatus.waiting.value
            )
        ]

    def _check_unchecked_event(
        self,
        event: UncheckedEvent,
        project: GitProject,
        service_config: ServiceConfig,
        job_configs: Iterable[JobConfig],
    ) -> bool:
        # Allowlist checks do not apply to CentOS (Pagure, GitLab) and distgit commit event.
        logger.info(f"{type(event)} event does not require allowlist checks.")
        return True

    def _check_release_push_event(
        self,
        event: Union[ReleaseEvent, PushGitHubEvent, PushGitlabEvent],
        project: GitProject,
        service_config: ServiceConfig,
        job_configs: Iterable[JobConfig],
    ) -> bool:
        # TODO: modify event hierarchy so we can use some abstract classes instead
        project_url = self._strip_protocol_and_add_git(event.project_url)
        if not project_url:
            raise KeyError(f"Failed to get namespace from {type(event)!r}")

        if self.is_approved(project_url):
            return True

        logger.info("Refusing release event on not allowlisted repo namespace.")
        return False

    def _check_pr_event(
        self,
        event: Union[
            PullRequestGithubEvent,
            PullRequestCommentGithubEvent,
            MergeRequestGitlabEvent,
            MergeRequestCommentGitlabEvent,
        ],
        project: GitProject,
        service_config: ServiceConfig,
        job_configs: Iterable[JobConfig],
    ) -> bool:
        actor_name = event.actor
        if not actor_name:
            raise KeyError(f"Failed to get login of the actor from {type(event)}")

        project_url = self._strip_protocol_and_add_git(event.project_url)

        namespace_approved = self.is_approved(project_url)
        user_approved = (
            project.can_merge_pr(actor_name)
            or project.get_pr(event.pr_id).author == actor_name
        )

        if namespace_approved and user_approved:
            # TODO: clear failing check when present
            return True

        msg = (
            f"Project {project_url} is not on our allowlist!"
            if not namespace_approved
            else f"Account {actor_name} has no write access nor is author of PR!"
        )
        logger.debug(msg)
        if isinstance(
            event, (PullRequestCommentGithubEvent, MergeRequestCommentGitlabEvent)
        ):
            project.get_pr(event.pr_id).comment(msg)
        else:
            for job_config in job_configs:
                job_helper = CoprBuildJobHelper(
                    service_config=service_config,
                    package_config=event.get_package_config(),
                    project=project,
                    metadata=EventData.from_event_dict(event.get_dict()),
                    db_trigger=event.db_trigger,
                    job_config=job_config,
                    build_targets_override=event.build_targets_override,
                    tests_targets_override=event.tests_targets_override,
                )
                msg = (
                    "Namespace is not allowed!"
                    if not namespace_approved
                    else "User cannot trigger!"
                )
                job_helper.report_status_to_all(
                    description=msg, state=BaseCommitStatus.neutral, url=FAQ_URL
                )

        return False

    def _check_issue_comment_event(
        self,
        event: Union[IssueCommentEvent, IssueCommentGitlabEvent],
        project: GitProject,
        service_config: ServiceConfig,
        job_configs: Iterable[JobConfig],
    ) -> bool:
        actor_name = event.actor
        if not actor_name:
            raise KeyError(f"Failed to get login of the actor from {type(event)}")
        project_url = self._strip_protocol_and_add_git(event.project_url)

        namespace_approved = self.is_approved(project_url)
        user_approved = project.can_merge_pr(actor_name)

        if namespace_approved and user_approved:
            return True

        msg = (
            f"Project {project_url} is not on our allowlist!"
            if not namespace_approved
            else f"Account {actor_name} has no write access!"
        )
        logger.debug(msg)
        project.get_issue(event.issue_id).comment(msg)
        return False

    def check_and_report(
        self,
        event: Optional[Any],
        project: GitProject,
        service_config: ServiceConfig,
        job_configs: Iterable[JobConfig],
    ) -> bool:
        """
        Check if account is approved and report status back in case of PR
        :param service_config: service config
        :param event: PullRequest and Release TODO: handle more
        :param project: GitProject
        :param job_configs: iterable of jobconfigs - so we know how to update status of the PR
        :return:
        """
        CALLBACKS: Dict[
            Union[type, Tuple[Union[type, Tuple[Any, ...]], ...]], Callable
        ] = {
            (  # events that are not checked against allowlist
                PushPagureEvent,
                PullRequestPagureEvent,
                PullRequestCommentPagureEvent,
                AbstractCoprBuildEvent,
                TestingFarmResultsEvent,
                InstallationEvent,
                KojiTaskEvent,
                KojiBuildEvent,
                CheckRerunEvent,
            ): self._check_unchecked_event,
            (
                ReleaseEvent,
                PushGitHubEvent,
                PushGitlabEvent,
            ): self._check_release_push_event,
            (
                PullRequestGithubEvent,
                PullRequestCommentGithubEvent,
                MergeRequestGitlabEvent,
                MergeRequestCommentGitlabEvent,
            ): self._check_pr_event,
            (
                IssueCommentEvent,
                IssueCommentGitlabEvent,
            ): self._check_issue_comment_event,
        }

        # Administrators
        user_login = getattr(  # some old events with user_login can still be there
            event, "user_login", None
        ) or getattr(event, "actor", None)

        if user_login and user_login in service_config.admins:
            logger.info(f"{user_login} is admin, you shall pass.")
            return True

        for related_events, callback in CALLBACKS.items():
            if isinstance(event, related_events):
                return callback(event, project, service_config, job_configs)

        msg = f"Failed to validate account: Unrecognized event type {type(event)!r}."
        logger.error(msg)
        raise PackitException(msg)
