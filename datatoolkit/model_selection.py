from abc import ABC, abstractmethod
from collections.abc import Callable, Generator, Iterable
from functools import partial
from itertools import product
from typing import Union

import numpy as np
import pandas as pd
import sklearn.metrics as sm
from hyperopt import STATUS_OK, Trials, fmin, hp, tpe
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.datasets import make_classification
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GridSearchCV, StratifiedShuffleSplit
from sklearn.exceptions import NotFittedError


class CostFunction(ABC):
    """Abstract class for cost functions"""

    def __init__(self, metrics: Iterable[str], M: "np.ndarray[float]") -> None:
        """Cost function constructor.

        Args:
            metrics (Iterable[str]): Iterable of strings of the form (metric_name).
            M (np.ndarray[float]): Positive definite matrix of size len(metrics).

        Raises:
            ValueError: _description_

        Returns:
            _type_: _description_
        """
        self.metrics = metrics
        self.M = M or np.identity(len(metrics))  # type: ignore
        self._check_positive_definite(self.M)

    @abstractmethod
    def objective(
        self, y_true: "np.ndarray[float]", y_pred: "np.ndarray[float]"
    ) -> float:
        """Objective function.

        Args:
            y_true (np.ndarray[float]): Array-like of true labels of length N.
            y_pred (np.ndarray[float]): Array-like of predicted labels of length N.
        """
        pass

    @staticmethod
    def _to_array(y: Iterable[float]) -> "np.ndarray[float]":
        return np.fromiter(y, float)

    @staticmethod
    def _check_positive_definite(M: "np.ndarray[float]") -> None:
        if not np.all(np.linalg.eigvals(M) > 0):
            raise ValueError(f"Matrix {M} is not positive definite")

    def make_scorer(self) -> Callable:
        return sm.make_scorer(self.objective, greater_is_better=False)

    def __call__(self, y_true: Iterable[float], y_pred: Iterable[float]) -> float:
        y_pred_array = self._to_array(y_pred)
        y_true_array = self._to_array(y_true)

        return self.objective(y_true_array, y_pred_array)


class ClassificationCostFunction(CostFunction):
    def __init__(
        self,
        metrics: Iterable[str],
        M: "np.ndarray[float]" = None,
        metric_class_opt_val_map: dict[str, tuple[str, float]] = None,
        proba_threshold: float = 0.5,
    ):
        """Defines cost functional for optimization of multiple metrics.
        Since this is defined as a loss function, cross validation returns the negative of the score [1].

        Args:
            metrics (Iterable[str]): Iterable of strings of the form (metric_name).
            M (np.ndarray[float]): Positive definite matrix of size len(metrics).
            metric_class_map (dict[str, str], optional): Dictionary mapping metric to class or probability of the form {'metric': 'class' or 'proba'}. Defaults to {}.
            proba_threshold (float, optional): Probability threshold used to convert probabilities into classes. Defaults to 0.5.

        References:
            [1] https://github.com/scikit-learn/scikit-learn/issues/2439

        Example:
            >>> y_true = [0, 0, 0, 1, 1]
            >>> y_pred = [0.46, 0.6, 0.29, 0.25, 0.012]
            >>> threshold = 0.5
            >>> metrics = ["f1_score", "roc_auc_score"]
            >>> cf = ClassificationCostFunction(metrics)
            >>> np.isclose(cf(y_true, y_pred), 1.41, rtol=1e-01, atol=1e-01)
            True
            >>> X, y = make_classification()
            >>> model = LogisticRegression()
            >>> model.fit(X, y)
            >>> y_proba = model.predict_proba(X)[:, 1]
            >>> cost = cf(y, y_proba)
            >>> f1 = getattr(sm, "f1_score")
            >>> roc_auc = getattr(sm, "roc_auc_score")
            >>> y_pred = np.where(y_proba > 0.5, 1, 0)
            >>> scorer_output = np.sqrt((f1(y, y_pred) - 1.0)**2 + (roc_auc(y, y_proba) - 1.0)**2)
            >>> np.isclose(cost, scorer_output)
            True
        """
        super().__init__(metrics, M)
        self.proba_threshold = proba_threshold
        self.metric_class_opt_val_map = metric_class_opt_val_map or {
            "accuracy_score": ("class", 1),
            "f1_score": ("class", 1),
            "log_loss": ("class", 0),
            "precision_score": ("class", 1),
            "recall_score": ("class", 1),
            "roc_auc_score": ("proba", 1),
        }

    def _to_class(self, array: "np.ndarray[float]", metric: str) -> "np.ndarray[float]":
        """Convert probability to class.

        Args:
            array (np.ndarray[float]): Array of probabilities of size (n_samples, 1).
            metric (str): Metric that requires class.

        Returns:
            np.ndarray[float]: Converted array of size (n_samples, 1).
        """
        # sourcery skip: inline-immediately-returned-variable
        output = (
            np.where(array > self.proba_threshold, 1, 0)
            if self.metric_class_opt_val_map[metric][0] == "class"
            else array
        )

        return output

    def objective(
        self, y_true: "np.ndarray[float]", y_pred: "np.ndarray[float]"
    ) -> float:

        self._check_positive_definite(self.M)

        opt_values = np.array(
            [self.metric_class_opt_val_map[metric][1] for metric in self.metrics]
        )

        metric_values = np.array(
            [
                getattr(sm, metric)(y_true, self._to_class(y_pred, metric))
                for metric in self.metrics
            ]
        )

        return np.sqrt(
            np.dot(
                np.dot(metric_values - opt_values, self.M), metric_values - opt_values
            )
        )


class BayesianSearchCV(BaseEstimator, ClassifierMixin):
    """_summary_

    Args:
        BaseEstimator (_type_): _description_
        ClassifierMixin (_type_): _description_

    Raises:
        TypeError: _description_
        NotFittedError: _description_
        NotFittedError: _description_

    Returns:
        _type_: _description_

    Yields:
        _type_: _description_

    References:
        [1] https://stackoverflow.com/questions/52408949/cross-validation-and-parameters-tuning-with-xgboost-and-hyperopt
    """

    def __init__(
        self,
        estimator,
        parameter_space: dict,
        n_iter: int = 10,
        scoring=Union[Iterable[str], Callable, None],
        cv=StratifiedShuffleSplit,
        refit: str = "loss",
        verbose=0,
        random_state=None,
        error_score="raise",
        return_train_score=False,
    ):
        self.estimator = estimator
        self.parameter_space = parameter_space
        self.cv = cv
        self.n_iter = n_iter
        self.random_state = random_state
        self.refit = refit
        self.scoring = scoring
        self.error_score = error_score
        self.return_train_score = return_train_score
        self.verbose = verbose

    def fit(self, X, y):
        # Instantiate estimator with optimized parameters
        self.n_splits = self.cv.get_n_splits(X, y)

        self.cv_results_ = {
            "parameters": [],
            "loss": [],
        }

        for (
            dataset_type_name,
            score_name,
            index,
        ) in self.get_dataset_type_score_name_index(range(self.n_splits)):
            self.cv_results_[f"{dataset_type_name}_{score_name}_split{index}"] = []

        _ = self.optimize(X, y)
        self.post_process_cv_results()

        self.best_index_ = np.argmin(self.cv_results_["rank_score"])
        self.best_params_ = self.cv_results_["parameters"][self.best_index_]

        if self.refit:
            best = self.estimator.set_params(**self.best_params_)
            best.fit(X, y)
            self.best_estimator_ = best

        return self

    @staticmethod
    def scorer_optimal_value(score_name: str) -> float:
        if score_name in {
            "accuracy_score",
            "balanced_accuracy_score",
            "top_k_accuracy_score",
            "average_precision_score",
            "neg_brier_score_score",
            "f1_score",
            "f1_micro_score",
            "f1_macro_score",
            "f1_weighted_score",
            "f1_samples_score",
            "precision_score",
            "recall_score",
            "jaccard_score",
            "roc_auc_score",
            "roc_auc_ovr_score",
            "roc_auc_ovo_score",
            "roc_auc_ovr_weighted_score",
            "roc_auc_ovo_weighted_score",
        }:
            return 1
        if score_name in {"neg_log_loss_score"}:
            return 0

    @staticmethod
    def scorer_class_map(
        y_pred: "np.ndarray[float]", score_name: str, threshold: float = 0.5
    ) -> "np.ndarray[float]":
        if score_name in {
            "accuracy_score",
            "balanced_accuracy_score",
            "top_k_accuracy_score",
            "average_precision_score",
            "neg_brier_score_score",
            "f1_score",
            "f1_micro_score",
            "f1_macro_score",
            "f1_weighted_score",
            "f1_samples_score",
            "neg_log_loss_score",
            "precision_score",
            "recall_score",
            "jaccard_score",
        }:
            return np.where(y_pred > threshold, 1, 0)
        if score_name in {
            "roc_auc_score",
            "roc_auc_ovr_score",
            "roc_auc_ovo_score",
            "roc_auc_ovr_weighted_score",
            "roc_auc_ovo_weighted_score",
        }:
            return y_pred

    def _raise_type_error(self):
        msg = f"scoring must be an iterable or a callable, got {type(self.scoring)}."
        raise TypeError(msg)

    def get_dataset_type_score_name_index(
        self, split_iterator: Union[Iterable[int], None] = None
    ) -> Generator[tuple[str, str, int]]:
        split_iterator = split_iterator or [1]

        if isinstance(self.scoring, Iterable):
            iterable = product({"train", "val"}, self.scoring, split_iterator)

        elif isinstance(self.scoring, Callable):
            iterable = product({"train", "val"}, ["score"], split_iterator)

        else:
            self._raise_type_error()

        yield from iterable

    def _check_refit_scoring(self) -> bool:
        if isinstance(self.scoring, Iterable):
            return self.refit in self.scoring

        else:
            return False

    def objective(
        self, y_true: Iterable[float], y_pred: Iterable[float], score_name: str
    ) -> float:

        scorer = getattr(sm, score_name)
        _y_pred = self.scorer_class_map(y_pred, score_name)

        return abs(scorer(y_true, _y_pred) - self.scorer_optimal_value(score_name))

    def post_process_cv_results(self):

        for (
            dataset_type_name,
            score_name,
            _,
        ) in self.get_dataset_type_score_name_index():
            scores_iterable = [
                self.cv_results_[f"{dataset_type_name}_{score_name}_split{index}"]
                for index in range(self.n_splits)
            ]
            self.cv_results_[f"average_{dataset_type_name}_{score_name}"] = np.mean(
                scores_iterable, axis=0
            )
            self.cv_results_[f"std_{dataset_type_name}_{score_name}"] = np.std(
                scores_iterable, axis=0
            )

        if self._check_refit_scoring():
            refit_col_name = f"average_val_{self.refit}"

        else:
            refit_col_name = "loss"

        ranks = list(range(len(self.cv_results_[refit_col_name])))
        for r, i in enumerate(
            sorted(ranks, key=lambda i: self.cv_results_[refit_col_name][i]), 1
        ):
            ranks[i] = r

        self.cv_results_["rank_score"] = ranks

    def cross_validate(self, parameter_space, X, y):
        # sourcery skip: remove-dict-keys

        original_parameters = self.estimator.get_params(deep=True)

        # Preserve the order in the following dict merge so that the values of `original_parameters` are replaced by the values of `parameter_space` when there is a key conflict.
        self.estimator.set_params(**{**original_parameters, **parameter_space})

        self.cv_results_["parameters"].append(parameter_space)

        loss = 0
        for index, (train_index, test_index) in enumerate(self.cv.split(X, y)):
            X_train, y_train = X[train_index], y[train_index]
            X_val, y_val = X[test_index], y[test_index]
            y_true = {"train": y_train, "val": y_val}
            self.estimator.fit(X_train, y_train)
            y_pred = {
                "train": self.estimator.predict_proba(X_train)[:, 1],
                "val": self.estimator.predict_proba(X_val)[:, 1],
            }

            for dataset_type_name in y_true.keys():
                if isinstance(self.scoring, Iterable):
                    for score_name in self.scoring:
                        score = self.objective(
                            y_true[dataset_type_name],
                            y_pred[dataset_type_name],
                            score_name,
                        )
                        self.cv_results_[
                            f"{dataset_type_name}_{score_name}_split{index}"
                        ].append(score)
                        loss += score

                elif isinstance(self.scoring, Callable):
                    for score_name in ["score"]:
                        score = self.scoring(
                            y_true[dataset_type_name], y_pred[dataset_type_name]
                        )
                        self.cv_results_[
                            f"{dataset_type_name}_{score_name}_split{index}"
                        ].append(score)
                        loss += score
                else:
                    self._raise_type_error()

        self.cv_results_["loss"].append(loss)

        return {"loss": loss, "status": STATUS_OK}

    def optimize(self, X, y):
        trials = Trials()
        return fmin(
            fn=partial(self.cross_validate, X=X, y=y),
            space=self.parameter_space,
            algo=tpe.suggest,
            max_evals=self.n_iter,
            trials=trials,
        )

    def predict_proba(self, X):
        if not hasattr(self, "best_estimator_"):
            raise NotFittedError("Call `fit` before `predict_proba`.")
        else:
            return self.best_estimator_.predict_proba(X)

    def predict(self, X):
        if not hasattr(self, "best_estimator_"):
            raise NotFittedError("Call `fit` before `predict`.")
        else:
            return self.best_estimator_.predict(X)
