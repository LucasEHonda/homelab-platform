import deployer


def test_package_imports():
    assert deployer.__name__ == "deployer"
