export PYTHONPATH=$PWD:$PYTHONPATH

# Run all tests in the file:
# pytest k8/dra-driver/test_dra_driver_install.py   --testbed=k8/dra-driver/cluster.yaml   --image-manifest=k8/dra-driver/dra-images.yaml   --deployment=k8 -v

# Run a single test case (add ::test_name after the file path):
pytest k8/dra-driver/test_dra_driver_install.py::test_dra_driver_install   --testbed=k8/dra-driver/cluster.yaml   --image-manifest=k8/dra-driver/dra-images.yaml   --deployment=k8 -v