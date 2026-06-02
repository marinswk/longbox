"""Shared volume-marker / series-name derivation."""

from app.services.series_naming import derive_series, strip_volume_marker


def test_derive_series_strips_numbered_markers():
    assert derive_series("Saga: Volume One") == "Saga"
    assert derive_series("Saga, Volume Five") == "Saga"
    assert derive_series("Saga, Book Two") == "Saga"
    assert derive_series("Locke & Key, Vol. 1") == "Locke & Key"
    assert derive_series("Immortal Hulk Vol. 3") == "Immortal Hulk"
    assert derive_series("The Sandman Vol. 1: Preludes & Nocturnes") == "The Sandman"
    assert derive_series("Immortal Hulk TPB") == "Immortal Hulk"


def test_derive_series_requires_number_after_marker():
    """A marker word inside the series name (no following number) must
    NOT truncate it — 'Blue Book' stays 'Blue Book', not 'Blue'."""
    assert derive_series("Blue Book Volume 2: 1947") == "Blue Book"
    assert derive_series("Blue Book") is None          # no volume → no derive
    assert derive_series("Free Comic Book Day 2018") is None


def test_derive_series_returns_none_without_marker():
    assert derive_series("Preludes & Nocturnes") is None
    assert derive_series("Saga, Compendium One") is None
    assert derive_series("Immortal Hulk Omnibus") is None
    assert derive_series("Locke & Key") is None
    assert derive_series(None) is None
    assert derive_series("") is None


def test_strip_volume_marker_keeps_original_without_marker():
    # Used by Metron's literal series filter — wants a query even when
    # there's nothing to strip.
    assert strip_volume_marker("saga vol 1") == "saga"
    assert strip_volume_marker("Saga") == "Saga"
    assert strip_volume_marker("Y The Last Man") == "Y The Last Man"
    assert strip_volume_marker("Blue Book Volume 2") == "Blue Book"
