
        // Modal Functions
        function openModal() {
            document.getElementById('customerModal').style.display = 'block';
            document.getElementById('modalTitle').textContent = 'Add New Customer';
            document.getElementById('customerForm').reset();
            document.getElementById('customerId').value = '';
            document.getElementById('customerForm').action = '/customer/add';
        }

        function closeModal() {
            document.getElementById('customerModal').style.display = 'none';
        }

        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('customerModal');
            if (event.target == modal) {
                modal.style.display = 'none';
            }
        }

       // Search and Filter
function filterTable() {
    const searchValue = document.getElementById('searchInput').value.toLowerCase();
    const statusFilter = document.getElementById('statusFilter').value.toLowerCase();
    const table = document.getElementById('customerTable');
    const rows = table.getElementsByTagName('tr');
    let visibleCount = 0;

    for (let i = 1; i < rows.length; i++) {
        const row = rows[i];
        const name = row.cells[1]?.textContent.toLowerCase() || '';
        const location = row.cells[2]?.textContent.toLowerCase() || '';
        const phone = row.cells[3]?.textContent.toLowerCase() || '';
        
        // Check if search matches
        const matchesSearch = name.includes(searchValue) || 
                            location.includes(searchValue) || 
                            phone.includes(searchValue);

        // Check if status filter matches
        let matchesStatus = true;
        if (statusFilter) {
            // Get the status checkboxes in the row
            const statusCell = row.cells[4]; // Status column (5th column, index 4)
            const checkbox = statusCell?.querySelector(`input[data-type="${statusFilter}"]`);
            matchesStatus = checkbox && checkbox.checked;
        }

        // Show row only if both search and status filter match
        if (matchesSearch && matchesStatus) {
            row.style.display = '';
            visibleCount++;
        } else {
            row.style.display = 'none';
        }
    }
}
     // Status Update
function updateStatus(customerId, statusType, isChecked) {
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/customer/status';
    
    const idInput = document.createElement('input');
    idInput.type = 'hidden';
    idInput.name = 'customer_id';
    idInput.value = customerId;
    
    const statusInput = document.createElement('input');
    statusInput.type = 'hidden';
    statusInput.name = 'status_type';
    statusInput.value = statusType;
    
    const checkedInput = document.createElement('input');
    checkedInput.type = 'hidden';
    checkedInput.name = 'is_checked';
    checkedInput.value = isChecked;
    
    form.appendChild(idInput);
    form.appendChild(statusInput);
    form.appendChild(checkedInput);
    document.body.appendChild(form);
    form.submit();
}

        // Edit Customer
        function editCustomer(customerId) {
            // You can fetch customer data via AJAX or pass it directly
            // For now, redirecting to edit page
            window.location.href = `/customer/edit/${customerId}`;
        }

        // Delete Customer
        function deleteCustomer(customerId) {
            if (confirm('Are you sure you want to delete this customer?')) {
                const form = document.createElement('form');
                form.method = 'POST';
                form.action = '/customer/delete';
                
                const idInput = document.createElement('input');
                idInput.type = 'hidden';
                idInput.name = 'customer_id';
                idInput.value = customerId;
                
                form.appendChild(idInput);
                document.body.appendChild(form);
                form.submit();
            }
        }