// Modal Functions
function openModal() {
    const modal = document.getElementById('customerModal');
    if (modal) {
        modal.style.display = 'block';
        document.getElementById('modalTitle').textContent = 'Add Customer Record';
        document.getElementById('customerForm').reset();
        document.getElementById('customerId').value = '';
        document.getElementById('customerForm').action = '/customer/add';
    }
}

function closeModal() {
    const modal = document.getElementById('customerModal');
    if (modal) modal.style.display = 'none';
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('customerModal');
    const viewModal = document.getElementById('viewModal');
    if (event.target === modal) {
        modal.style.display = 'none';
    }
    if (event.target === viewModal) {
        viewModal.style.display = 'none';
    }
}

// Search and Filter
function filterTable() {
    const searchValue = (document.getElementById('searchInput')?.value || '').toLowerCase().trim();
    const statusFilter = (document.getElementById('statusFilter')?.value || '').toLowerCase();
    const typeFilter = (document.getElementById('typeFilter')?.value || '').toLowerCase();
    const table = document.getElementById('customerTable');
    if (!table) return;

    const rows = table.getElementsByTagName('tr');
    let visibleCount = 0;

    for (let i = 1; i < rows.length; i++) {
        const row = rows[i];
        if (!row.cells || row.cells.length < 5) continue;

        // Search text across all cell content in row
        const rowText = row.textContent.toLowerCase();
        const matchesSearch = !searchValue || rowText.includes(searchValue);

        // Check status filter (Call, SMS, WhatsApp, Email checkboxes)
        let matchesStatus = true;
        if (statusFilter) {
            const checkbox = row.querySelector(`input[data-type="${statusFilter}"]`);
            matchesStatus = checkbox && checkbox.checked;
        }

        // Check submission type filter (order, inquiry, customer)
        let matchesType = true;
        if (typeFilter) {
            const rowType = (row.getAttribute('data-type') || '').toLowerCase();
            matchesType = rowType.includes(typeFilter);
        }

        if (matchesSearch && matchesStatus && matchesType) {
            row.style.display = '';
            visibleCount++;
        } else {
            row.style.display = 'none';
        }
    }

    const totalEl = document.getElementById('totalCustomers');
    if (totalEl) totalEl.textContent = visibleCount;
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
    window.location.href = `/customer/edit/${customerId}`;
}

// Delete Customer
function deleteCustomer(customerId) {
    if (confirm('Are you sure you want to delete this record?')) {
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
